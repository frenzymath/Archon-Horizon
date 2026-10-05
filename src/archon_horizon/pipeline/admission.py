"""Recorded run limits affect admission, never semantic completion."""

from __future__ import annotations

from sqlalchemy import func, select

from .schema import tables
from .usage_accounting import trusted_amount, uncertain_usage


def run_usage(conn, run):
    assignment = tables["assignment"]
    supervisor = assignment.c.functions.contains(["orchestrator"])
    admissions, supervisors = conn.execute(select(
        func.count().filter(~supervisor), func.count().filter(supervisor)).select_from(assignment).where(
            assignment.c.run_id == run["id"], assignment.c.started_at.is_not(None))).one()
    tokens = None
    uncertain = False
    if run["token_budget"] is not None:
        usage, execution = tables["usage_record"], tables["execution"]
        measured = conn.execute(select(func.coalesce(func.sum(func.coalesce(trusted_amount(usage, "input_tokens"), 0)
            + func.coalesce(trusted_amount(usage, "output_tokens"), 0)), 0).label("tokens"),
            func.coalesce(func.bool_or(uncertain_usage(usage)), False).label("uncertain"))
            .select_from(usage.join(execution).join(assignment)).where(assignment.c.run_id == run["id"])).mappings().one()
        tokens, uncertain = measured["tokens"], measured["uncertain"]
    return {"admitted_assignments": admissions, "supervision_assignments": supervisors,
            "observed_tokens": tokens, "usage_uncertain": uncertain}


def reason(run, usage, now, *, already_started=False, supervisory=False):
    if run["expires_at"] and run["expires_at"] <= now:
        return "Run admission deadline reached"
    if run["token_budget"] is not None and usage["observed_tokens"] >= run["token_budget"]:
        return "Run token budget reached; unfinished work remains recorded"
    if run["token_budget"] is not None and usage.get("usage_uncertain"):
        return "Provider usage needs reconciliation before token-budgeted work can resume"
    # Supervision shares the explicit token/deadline limits, but cannot consume
    # or be starved by the productive assignment allowance it supervises.
    if not supervisory and not already_started and run["max_assignments"] is not None and usage["admitted_assignments"] >= run["max_assignments"]:
        return "Run assignment limit reached; existing contexts may still resume"
    return None
