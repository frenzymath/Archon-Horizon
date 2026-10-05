"""Counter reconciliation and SQL expressions for honest usage aggregates.

Raw counters are immutable evidence. Only measured deltas are additive; unknown
scope/reset observations remain visible and cannot make a finite budget free.
"""

from decimal import Decimal

from sqlalchemy import DateTime, and_, case, exists, func, not_, or_, select

COUNTERS = ("input_tokens", "cached_input_tokens", "output_tokens", "cost_usd")


def reconcile_counter(values, previous, observed_at):
    """Return additive values and an immutable checkpoint for one counter epoch."""
    raw = {key: str(value) if key == "cost_usd" and value is not None else value
           for key, value in values.items()}
    evidence = {"raw": raw, "checkpoint": raw, "observed_at": observed_at, "status": "measured"}
    if previous is None:
        evidence["status"] = "initial_session_total"
        return values, evidence
    prior = {**previous.get("checkpoint", previous["raw"])}
    if prior.get("cost_usd") is not None:
        prior["cost_usd"] = Decimal(prior["cost_usd"])
    if observed_at < previous["observed_at"]:
        contradicts_later = any(value is not None and prior.get(key) is not None and value > prior[key]
                                for key, value in values.items())
        evidence["status"] = "counter_reset_unknown" if contradicts_later else "out_of_order"
        return dict.fromkeys(COUNTERS), evidence
    decreased = any(value is not None and prior.get(key) is not None and value < prior[key]
                    for key, value in values.items())
    if previous["status"] == "counter_reset_unknown" or decreased:
        evidence["status"] = "counter_reset_unknown"
        return dict.fromkeys(COUNTERS), evidence
    # A missing field does not erase its last known total. The first observation
    # of a field accounts its session total once, just like the first snapshot.
    delta = {key: value - (prior.get(key) or 0) if value is not None else None
             for key, value in values.items()}
    evidence["checkpoint"] = {key: raw[key] if raw[key] is not None else previous.get("checkpoint", previous["raw"]).get(key)
                              for key in raw}
    return delta, evidence


def _legacy_codex(usage):
    return and_(usage.c.accounting.is_(None), usage.c.provider_record_id.startswith("codex:"),
                not_(usage.c.provider_record_id.startswith("codex:cumulative:")))


def trusted_amount(usage, key):
    """SQL additive amount; never display historical ambiguous Codex totals as cost."""
    if key not in COUNTERS:
        raise ValueError("unknown usage counter")
    return case((_legacy_codex(usage), None), else_=usage.c[key])


def uncertain_usage(usage):
    """SQL uncertainty flag, including old totals until native counters cover them."""
    from .schema import tables

    authority = tables["usage_record"].alias("usage_authority")
    thread = tables["provider_thread"].alias("usage_native_thread")
    native_identity = select(thread.c.provider_thread_id).where(
        thread.c.id == usage.c.provider_thread_id).correlate(usage).scalar_subquery()
    identity = func.coalesce(usage.c.accounting["identity"].astext, native_identity)
    covered = exists(select(authority.c.id).where(
        authority.c.accounting["identity"].astext == identity,
        authority.c.accounting["scope"].astext == "native_thread_cumulative",
        authority.c.accounting["epoch"].astext == func.coalesce(usage.c.accounting["epoch"].astext, "session"),
        authority.c.accounting["status"].astext.in_(("measured", "initial_session_total")),
        authority.c.accounting["observed_at"].astext.cast(DateTime(timezone=True)) >= usage.c.created_at,
        authority.c.accounting["raw"]["input_tokens"].astext.is_not(None),
        authority.c.accounting["raw"]["output_tokens"].astext.is_not(None)).correlate(usage))
    unknown = usage.c.accounting["status"].astext.in_(("unknown_scope", "counter_reset_unknown", "partial_baseline"))
    missing_native = and_(usage.c.accounting["scope"].astext == "native_thread_cumulative", or_(
        usage.c.accounting["checkpoint"]["input_tokens"].astext.is_(None),
        usage.c.accounting["checkpoint"]["output_tokens"].astext.is_(None)))
    return or_(and_(_legacy_codex(usage), not_(covered)),
               and_(missing_native, not_(covered)),
               and_(unknown, or_(usage.c.accounting["status"].astext != "unknown_scope", not_(covered))))
