"""Bounded native observations, with accounting independent of transcript rendering.

Usage sources identify their counter scope; overlapping cumulative snapshots
share one checkpoint. Native children are observations of provider work, never
additional scheduler submissions or permission grants.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from uuid import UUID, uuid5

from .harness_events import HarnessEventKind
from .harness_parsers import parse_claude_line, parse_codex_line

from .errors import DomainError


_COUNTERS = ("input_tokens", "cached_input_tokens", "output_tokens", "cost_usd")
_TERMINAL = {"completed": "completed", "succeeded": "completed", "failed": "failed",
             "errored": "failed", "cancelled": "interrupted", "interrupted": "interrupted",
             "shutdown": "interrupted"}
_REVIEW_PROMPT_LABEL = re.compile(r"^Review label: (hz_review_[a-f0-9]{32})[ \t]*$", re.MULTILINE)


def _text(value, limit=256):
    return value[:limit] if isinstance(value, str) and value else None


def _count(value):
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else None


def _cost(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        return None
    try:
        result = Decimal(str(value))
        return result.quantize(Decimal("0.000000000001")) if result.is_finite() and 0 <= result < 10**12 else None
    except InvalidOperation:
        return None


def _usage(raw, *, claude=False):
    raw = raw if isinstance(raw, dict) else {}
    incoming = _count(raw.get("input_tokens", raw.get("tokens_in")))
    cached = _count(raw.get("cache_read_input_tokens" if claude else "cached_input_tokens",
                            raw.get("cached_tokens_in")))
    if claude and incoming is not None:
        # Claude reports uncached input separately; Codex input already includes cache hits.
        incoming += (cached or 0) + (_count(raw.get("cache_creation_input_tokens")) or 0)
        incoming = _count(incoming)
    return {"input_tokens": incoming, "cached_input_tokens": cached,
            "output_tokens": _count(raw.get("output_tokens", raw.get("tokens_out"))),
            "cost_usd": _cost(raw.get("cost_usd"))}


def _usage_fields(raw):
    if not isinstance(raw, dict):
        return {}
    keys = ("input_tokens", "output_tokens", "cached_input_tokens", "cache_read_input_tokens",
            "cache_creation_input_tokens", "reasoning_output_tokens", "tokens_in", "tokens_out",
            "cached_tokens_in", "cost_usd")
    return {key: raw[key] for key in keys if isinstance(raw.get(key), (int, float, str))
            and len(str(raw[key])) <= 64}


def select_native_event(adapter: str, raw: dict) -> dict | None:
    """Keep accounting/lifecycle fields, excluding prompts, tool output and answers."""
    if not isinstance(raw, dict):
        return None
    kind = raw.get("type")
    selected = {"type": kind}
    for key in ("uuid", "id", "turn_id", "session_id", "parent_tool_use_id"):
        if value := _text(raw.get(key)):
            selected[key] = value
    if adapter == "codex" and kind in ("turn.completed", "horizon.usage_snapshot"):
        selected["usage"] = _usage_fields(raw.get("usage"))
        accounting = raw.get("accounting")
        if isinstance(accounting, dict):
            selected["accounting"] = {key: _text(accounting.get(key)) for key in (
                "scope", "identity", "epoch", "source", "provider_version")}
    elif adapter == "codex" and kind == "item.completed":
        item = raw.get("item")
        if not isinstance(item, dict) or item.get("type") != "collab_tool_call":
            return None
        slim = {"type": "collab_tool_call"}
        for key in ("id", "tool", "sender_thread_id", "task_name", "description", "agent_type", "agent_nickname", "model", "reasoning_effort"):
            if value := _text(item.get(key)):
                slim[key] = value
        if item.get("tool") == "spawn_agent" and not slim.get("task_name") and isinstance(item.get("prompt"), str):
            # Codex may omit task_name from stdout. Retain only the canonical
            # prepared label, never its surrounding review prompt.
            labels = _REVIEW_PROMPT_LABEL.finditer(item["prompt"])
            label = next(labels, None)
            if label is not None and next(labels, None) is None:
                slim["task_name"] = label.group(1)
        receivers = item.get("receiver_thread_ids", item.get("receiver_thread_id", []))
        if isinstance(receivers, str):
            receivers = [receivers]
        slim["receiver_thread_ids"] = [value for value in (_text(v) for v in receivers[:128]) if value] if isinstance(receivers, list) else []
        states = item.get("agents_states")
        slim["agents_states"] = {}
        for key, state in list(states.items())[:128] if isinstance(states, dict) else []:
            if not isinstance(state, dict) or not _text(key):
                continue
            status = state.get("status")
            if isinstance(status, dict):
                status = next(iter(status), "running")
            slim["agents_states"][key[:256]] = {"status": _text(status), "usage": _usage_fields(state.get("usage")),
                "model": _text(state.get("model")), "effort": _text(state.get("effort"))}
        selected["item"] = slim
    elif adapter == "claude" and kind in ("assistant", "user"):
        message = raw.get("message")
        if not isinstance(message, dict):
            return None
        slim = {"id": _text(message.get("id")), "usage": _usage_fields(message.get("usage")), "content": []}
        content = message.get("content")
        for block in content[:128] if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") in ("Agent", "Task"):
                args = block.get("input") if isinstance(block.get("input"), dict) else {}
                slim["content"].append({"type": "tool_use", "id": _text(block.get("id")), "name": block["name"],
                    "input": {**{key: _text(args.get(key)) for key in ("subagent_type", "model", "description")},
                              "run_in_background": args.get("run_in_background") is True}})
            elif block.get("type") == "tool_result":
                slim["content"].append({"type": "tool_result", "tool_use_id": _text(block.get("tool_use_id")),
                                        "is_error": block.get("is_error") is True})
        if not slim["usage"] and not slim["content"]:
            return None
        selected["message"] = slim
    elif adapter == "claude" and kind == "result":
        selected["usage"] = _usage_fields(raw.get("usage"))
        selected["is_error"] = raw.get("is_error") is True
        for key in ("cost_usd", "total_cost_usd"):
            if (value := _cost(raw.get(key))) is not None:
                selected[key] = str(value)
    elif adapter == "claude" and kind == "queue-operation":
        # Reuse the established notification parser, retaining no arbitrary queue text.
        try:
            events = parse_claude_line(json.dumps(raw))
        except (TypeError, ValueError, AttributeError, OverflowError):
            return None
        children = [{"key": event.data.get("subagent_key"), "status": event.data.get("status")}
                    for event in events if event.kind == HarnessEventKind.SUBAGENT_END]
        return {"type": "horizon.child_notifications", "children": children[:128]} if children else None
    elif kind == "horizon.child_notifications":
        notifications = raw.get("children")
        selected["children"] = [{"key": _text(item.get("key")), "status": _text(item.get("status")),
                                 **({"discovered": True} if item.get("discovered") is True else {}),
                                 **({"native_id": _text(item["native_id"])} if item.get("native_id") else {}),
                                 **({"invocation_id": _text(item["invocation_id"])} if item.get("invocation_id") else {})}
                                for item in notifications[:128] if isinstance(item, dict)] if isinstance(notifications, list) else []
    else:
        return None
    return selected


@dataclass(frozen=True)
class UsageSample:
    key: str
    values: dict
    child_key: str | None = None
    cumulative: bool = False
    fallback: bool = False
    accounting: dict | None = None


@dataclass(frozen=True)
class ChildObservation:
    key: str
    status: str
    invocation_id: str | None = None
    model: str | None = None
    known_only: bool = False
    parent_key: str | None = None
    background: bool | None = None
    invocation_ref: UUID | None = None
    label: str | None = None
    description: str | None = None
    native_id: str | None = None


def _invocation_ref(*values):
    for value in values:
        if isinstance(value, str) and value.startswith("hz_review_"):
            candidate = value.split()[0][len("hz_review_"):]
            if len(candidate) == 32:
                try:
                    return UUID(hex=candidate)
                except ValueError:
                    pass
    return None


def normalize(adapter: str, raw: dict, event_id: str) -> tuple[list[UsageSample], list[ChildObservation]]:
    selected = select_native_event(adapter, raw)
    if selected is None:
        return [], []
    kind = selected["type"]
    usages, children = [], []
    child_key = selected.get("parent_tool_use_id") if adapter == "claude" else None
    if adapter == "codex" and kind in ("turn.completed", "horizon.usage_snapshot"):
        evidence = selected.get("accounting", {})
        known = (evidence.get("scope") == "native_thread_cumulative" and evidence.get("identity")
                 and evidence.get("source") in ("rollout_total_token_usage", "rollout_correlated_completion"))
        usages.append(UsageSample("native_total" if known else "turn:" + (
            selected.get("turn_id") or selected.get("id") or event_id), _usage(selected["usage"]),
            cumulative=bool(known), accounting={**evidence,
                "scope": "native_thread_cumulative" if known else "unknown",
                "epoch": evidence.get("epoch") or "session"}))
    if adapter == "claude" and kind == "assistant":
        message = selected["message"]
        if message.get("id") and message["usage"]:
            usages.append(UsageSample("message:" + message["id"], _usage(message["usage"], claude=True), child_key, cumulative=True))
    if adapter == "claude" and kind == "result":
        # Results can include children. They are fallback token evidence, never added
        # on top of authoritative per-message samples from the same invocation.
        values = _usage(selected["usage"], claude=True)
        values["cost_usd"] = None
        usages.append(UsageSample("result", values, child_key, fallback=True))
        if "total_cost_usd" in selected:
            usages.append(UsageSample("total_cost", {**dict.fromkeys(_COUNTERS), "cost_usd": _cost(selected["total_cost_usd"])}, child_key, cumulative=True))
        elif "cost_usd" in selected:
            usages.append(UsageSample("result_cost", {**dict.fromkeys(_COUNTERS), "cost_usd": _cost(selected["cost_usd"])}, child_key))
    parser = parse_codex_line if adapter == "codex" else parse_claude_line
    try:
        events = parser(json.dumps(selected)) if kind != "horizon.child_notifications" else []
    except (TypeError, ValueError, AttributeError, OverflowError):
        events = []
    for event in events:
        key = _text(event.data.get("subagent_key"))
        if key and event.kind in (HarnessEventKind.SUBAGENT_START, HarnessEventKind.SUBAGENT_END):
            item = selected.get("item", {})
            invocation = item.get("id") if item.get("tool") in ("spawn_agent", "send_input") else None
            children.append(ChildObservation(key, _TERMINAL.get(event.data.get("status"), "running"), invocation,
                                             _text(event.data.get("model")), parent_key=child_key,
                                             background=event.data.get("background"), invocation_ref=_invocation_ref(
                                                 item.get("task_name"), event.data.get("name"), event.data.get("description")),
                                             label=_text(event.data.get("name")) if event.kind == HarnessEventKind.SUBAGENT_START else None,
                                             description=_text(event.data.get("description"), 1600) if event.kind == HarnessEventKind.SUBAGENT_START else None))
            if isinstance(event.data.get("usage"), dict):
                usages.append(UsageSample("child_total", _usage(event.data["usage"]), key, cumulative=True))
        elif adapter == "claude" and event.kind == HarnessEventKind.TOOL_RESULT and event.data.get("call_id"):
            children.append(ChildObservation(event.data["call_id"], "failed" if event.data.get("is_error") else "completed", known_only=True))
    if kind == "horizon.child_notifications":
        children.extend(ChildObservation(item["key"], _TERMINAL.get(item["status"], "running"),
                        invocation_id=item.get("invocation_id"),
                        known_only=adapter == "codex" and (not item.get("discovered") or bool(
                            _invocation_ref(item["key"].rsplit("/", 1)[-1]))),
                        invocation_ref=_invocation_ref(item["key"].rsplit("/", 1)[-1]) if item.get("discovered") else None,
                        native_id=item.get("native_id"),
                        label=item["key"].rsplit("/", 1)[-1] if item.get("discovered") else None)
                        for item in selected["children"] if item["key"])
    if adapter == "codex" and selected.get("item", {}).get("tool") == "send_input":
        item = selected["item"]
        known = {child.key for child in children}
        children.extend(ChildObservation(key, "running", item.get("id"))
                        for key in item.get("receiver_thread_ids", []) if key not in known)
    if child_key:
        children.insert(0, ChildObservation(child_key, "running"))
    return usages, children


def child_state_ref(parent_thread_id: UUID, adapter: str, native_key: str) -> str:
    digest = hashlib.sha256(native_key.encode()).hexdigest()
    return f"native-child:{parent_thread_id}:{adapter}:{digest}"


def project_observation(conn, actor, execution: dict, thread: dict, request_id, adapter: str,
                        raw: dict, operation_id: UUID, observed, project_id: UUID) -> dict:
    """Project under the caller's mutation lock; retain late observations after fencing."""
    from .records import create, emit, get

    request = get(conn, "provider_request", request_id, lock=True)
    if request["execution_id"] != execution["id"] or request["provider_thread_id"] != thread["id"]:
        raise DomainError("scope_mismatch", "Provider observation belongs to another request", 422)
    harness_revision = get(conn, "record_revision", thread["harness_revision_id"])
    if adapter not in ("codex", "claude") or harness_revision["content"].get("adapter") != adapter + "_exec":
        raise DomainError("adapter_mismatch", "Provider observation must use the pinned adapter", 422)
    usages, children = normalize(adapter, raw, str(operation_id))
    child_rows = {}
    changes = 0
    for observation in children:
        child = _child(conn, execution, thread, request, adapter, observation, observed)
        if child is None:
            continue
        child_rows[observation.key] = child[0]
        if child[2]:
            changes += 1
            activity = create(conn, "activity", assignment_id=execution["assignment_id"], execution_id=execution["id"],
                provider_thread_id=child[0]["id"], provider_request_id=child[1]["id"],
                kind="failure" if child[1]["status"] in ("failed", "interrupted") else (
                    "completion" if child[1]["status"] == "completed" else "progress"),
                summary=f"Native child {child[0]['number']}: {child[1]['status']}", occurred_at=observed)
            emit(conn, actor.id, project_id, "activity", activity, ["kind"], execution_id=execution["id"])
    added = 0
    for sample in usages:
        target = child_rows.get(sample.child_key) if sample.child_key else thread
        if target is None:
            child = _child(conn, execution, thread, request, adapter, ChildObservation(sample.child_key, "running"), observed)
            target = child[0]
        if _record_usage(conn, execution, request, target, adapter, sample, operation_id, observed):
            added += 1
    result = {"usage_records": added, "child_changes": changes}
    from .activity_display import activity_identity, display_event, skills_referenced
    from sqlalchemy import select
    from .schema import tables

    display = display_event(adapter, raw)
    if display:
        identity = activity_identity(request_id, display, operation_id)
        if not conn.execute(select(tables["activity"].c.id).where(tables["activity"].c.id == identity)).first():
            activity = create(conn, "activity", id=identity, assignment_id=execution["assignment_id"],
                execution_id=execution["id"], provider_thread_id=thread["id"], provider_request_id=request["id"],
                kind="tool_use" if display["category"] == "tool" else ("failure" if display["category"] == "failure" else "progress"),
                summary=display["title"], skills_used=skills_referenced(display), occurred_at=observed)
            emit(conn, actor.id, project_id, "activity", activity, ["kind", "summary", "skills_used"], execution_id=execution["id"])
        result["activity_id"] = identity
    return result


def _child(conn, execution, parent, root_request, adapter, observation, observed):
    from sqlalchemy import select
    from .records import change, create, next_number
    from .schema import tables
    from .reviewer_invocations import account_observed_child, bind_native

    table = tables["provider_thread"]
    if observation.invocation_ref:
        request_table = tables["provider_request"]
        registered = conn.execute(select(request_table.c.id).where(request_table.c.id == observation.invocation_ref,
            request_table.c.execution_id == execution["id"], request_table.c.reason == "review")).scalar_one_or_none()
        if registered:
            try:
                with conn.begin_nested():
                    bind_native(conn, execution, parent, registered, adapter, observation.native_id or observation.key,
                                observation.invocation_id, observation.background)
            except DomainError:
                # A mismatched/cancelled registration does not hide actual provider
                # work. Record it as an unregistered child with no review authority.
                pass
    state_ref = child_state_ref(parent["id"], adapter, observation.key)
    predicate = table.c.provider_state_ref == state_ref
    if adapter == "codex":
        predicate = predicate | (table.c.provider_thread_id == (observation.native_id or observation.key))
    row = conn.execute(select(table).where(table.c.assignment_id == parent["assignment_id"], table.c.kind == "child", predicate)
                       .with_for_update()).mappings().first()
    if row is None and adapter == "codex" and observation.known_only:
        reference = _invocation_ref(observation.key.rsplit("/", 1)[-1])
        if reference:
            requests = tables["provider_request"]
            review_parent = requests.alias("review_parent")
            conditions = [
                requests.c.id == reference, requests.c.execution_id == execution["id"],
                requests.c.reason == "review", table.c.kind == "child",
                table.c.assignment_id == parent["assignment_id"],
                review_parent.c.provider_thread_id == parent["id"],
                review_parent.c.execution_id == execution["id"],
                table.c.provider_thread_id.is_not(None),
            ]
            if observation.native_id:
                conditions.append(table.c.provider_thread_id == observation.native_id)
            row = conn.execute(select(table).join(requests, requests.c.provider_thread_id == table.c.id)
                               .join(review_parent, review_parent.c.id == table.c.parent_request_id)
                               .where(*conditions)).mappings().first()
    if row is None and observation.known_only:
        return None
    changed = row is None
    parent_request_id = root_request["id"]
    if observation.parent_key:
        nested_parent = conn.execute(select(table.c.id).where(table.c.assignment_id == parent["assignment_id"],
            table.c.provider_state_ref == child_state_ref(parent["id"], adapter, observation.parent_key))).scalar_one_or_none()
        if nested_parent:
            requests = tables["provider_request"]
            parent_request_id = conn.execute(select(requests.c.id).where(requests.c.provider_thread_id == nested_parent)
                                              .order_by(requests.c.number.desc()).limit(1)).scalar_one()
    child = dict(row) if row else create(conn, "provider_thread", assignment_id=parent["assignment_id"],
        number=next_number(conn, "provider_thread", "assignment_id", parent["assignment_id"]), kind="child",
        parent_request_id=parent_request_id, workspace_id=parent["workspace_id"],
        harness_revision_id=parent["harness_revision_id"], skill_bundle_artifact_id=parent["skill_bundle_artifact_id"],
        provider_thread_id=(observation.native_id or observation.key) if adapter == "codex" else None, provider_state_ref=state_ref,
        applied_model_options={"model": observation.model} if observation.model else {}, status="available")
    requests = tables["provider_request"]
    metadata = {key: value for key, value in (("label", observation.label), ("description", observation.description))
                if value and value != child.get(key)}
    if metadata:
        child = change(conn, "provider_thread", child["id"], **metadata)
        changed = True
    row = conn.execute(select(requests).where(requests.c.provider_thread_id == child["id"])
                       .order_by(requests.c.number.desc()).limit(1)).mappings().first()
    child_request = dict(row) if row else None
    if observation.native_id and (not child_request or not child_request["reviewer_descriptor_id"]):
        if child["provider_state_ref"] != state_ref or child["provider_thread_id"] != observation.native_id:
            child = change(conn, "provider_thread", child["id"], provider_state_ref=state_ref,
                           provider_thread_id=observation.native_id)
            changed = True
    invocation = observation.invocation_id
    if invocation:
        original = conn.execute(select(requests).where(requests.c.provider_thread_id == child["id"],
            requests.c.provider_turn_id == invocation).order_by(requests.c.number).limit(1)).mappings().first()
        if original:
            child_request = dict(original)
    if child_request and observation.background is not None and child_request["native_background"] is None:
        child_request = change(conn, "provider_request", child_request["id"], native_background=observation.background)
    if child_request and observation.known_only and child_request["execution_id"] != execution["id"]:
        return None
    if adapter == "claude" and child_request and observation.known_only and observation.status == "completed" and child_request["native_background"] is not False:
        account_observed_child(conn, execution, child_request, observed)
        return child, child_request, changed
    new_invocation = invocation and child_request and invocation != child_request["provider_turn_id"]
    if child_request is None or (new_invocation and child_request["status"] in _TERMINAL):
        child_request = create(conn, "provider_request", provider_thread_id=child["id"], execution_id=execution["id"],
            number=next_number(conn, "provider_request", "provider_thread_id", child["id"]), reason="continuation",
            provider_turn_id=invocation, status=observation.status, submitted_at=observed, started_at=observed,
            native_background=observation.background,
            finished_at=observed if observation.status in _TERMINAL else None)
        changed = True
    elif observation.status in _TERMINAL and child_request["status"] not in _TERMINAL and (
            child_request["started_at"] is None or observed >= child_request["started_at"]):
        child_request = change(conn, "provider_request", child_request["id"], status=observation.status, finished_at=observed)
        changed = True
    elif observation.status == "running" and child_request["status"] in ("pending", "submitted"):
        child_request = change(conn, "provider_request", child_request["id"], status="running", started_at=observed,
                               provider_turn_id=child_request["provider_turn_id"] or invocation)
        changed = True
    # Descriptors are retained only on a server-registered request; event labels
    # and model-generated names cannot manufacture reviewer provenance.
    account_observed_child(conn, execution, child_request, observed)
    return child, child_request, changed


def _record_usage(conn, execution, request, thread, adapter, sample, operation_id, observed):
    from sqlalchemy import func, select
    from .records import create
    from .schema import tables

    table = tables["usage_record"]
    if sample.accounting is not None and not any(value is not None for value in sample.values.values()):
        return False
    key = hashlib.sha256(sample.key.encode()).hexdigest()
    prefix = f"{adapter}:{'cumulative' if sample.cumulative else request['id']}:{key}:"
    record_id = prefix + str(operation_id) if sample.cumulative else prefix
    if conn.execute(select(table.c.id).where(table.c.provider_thread_id == thread["id"], table.c.provider_record_id == record_id)).first():
        return False
    if sample.fallback or sample.key in ("total_cost", "result_cost"):
        children = tables["provider_thread"]
        if conn.execute(select(children.c.id).where(children.c.parent_request_id == request["id"]).limit(1)).first():
            return False
        if sample.fallback:
            # Message samples are cumulative and tied to this execution. Result
            # fallback only applies when no finer-grained usage was observed.
            activity = tables["activity"]
            if conn.execute(select(table.c.id).join(activity, activity.c.usage_record_id == table.c.id).where(
                    activity.c.provider_request_id == request["id"],
                    table.c.provider_thread_id == thread["id"], table.c.provider_record_id.startswith(f"{adapter}:cumulative:"),
                    table.c.input_tokens.is_not(None)).limit(1)).first():
                return False
    values = dict(sample.values)
    accounting = None
    previous = None
    if sample.accounting is not None:
        from .usage_accounting import reconcile_counter

        accounting = {**sample.accounting,
                      "raw": {key: str(value) if isinstance(value, Decimal) else value for key, value in values.items()},
                      "observed_at": observed.isoformat(),
                      "status": "unknown_scope"}
        if accounting["scope"] == "native_thread_cumulative":
            identity, epoch = accounting["identity"], accounting["epoch"]
            # Recovered Horizon thread rows can refer to the same native session.
            # Serialize by that identity, not the mutable execution/request row.
            lock_key = int.from_bytes(hashlib.sha256((identity + ":" + epoch).encode()).digest()[:8], "big", signed=True)
            conn.execute(select(func.pg_advisory_xact_lock(lock_key)))
            previous = conn.execute(select(table.c.accounting).where(
                table.c.accounting["identity"].astext == identity,
                table.c.accounting["epoch"].astext == epoch,
                table.c.accounting["scope"].astext == "native_thread_cumulative",
                table.c.accounting["status"].astext != "out_of_order")
                .order_by((table.c.accounting["status"].astext == "counter_reset_unknown").desc(),
                          table.c.accounting["observed_at"].astext.desc(), table.c.created_at.desc())
                .limit(1)).scalar_one_or_none()
            if (previous and previous["raw"] == values and previous["status"] != "counter_reset_unknown"
                    and observed.isoformat() <= previous["observed_at"]):
                return False
            values, checkpoint = reconcile_counter(values, previous, observed.isoformat())
            accounting.update(checkpoint)
        else:
            values = dict.fromkeys(_COUNTERS)
    elif sample.cumulative:
        threads = tables["provider_thread"]
        # A reconciled duplicate retains its immutable usage on the old thread.
        # Include that exact history when continuing the same child's counters.
        history = select(threads.c.id).where(threads.c.assignment_id == thread["assignment_id"],
            (threads.c.id == thread["id"]) | (threads.c.provider_state_ref == f"reconciled-review:{thread['id']}"))
        previous = conn.execute(select(*(func.sum(table.c[key]).label(key) for key in _COUNTERS)).where(
            table.c.provider_thread_id.in_(history), table.c.provider_record_id.startswith(prefix))).mappings().one()
        values = {key: max(0, value - (previous[key] or 0)) if value is not None else None for key, value in values.items()}
    if not accounting and not any(value is not None for value in values.values()):
        return False
    if not accounting and previous and not any(value for value in values.values()) and all(
            value is None or previous[key] is not None for key, value in values.items()):
        return False
    usage = create(conn, "usage_record", id=uuid5(thread["id"], record_id), execution_id=execution["id"],
                   provider_thread_id=thread["id"], provider_record_id=record_id, accounting=accounting, **values)
    activity_request = request["id"]
    if thread["id"] != request["provider_thread_id"]:
        requests = tables["provider_request"]
        activity_request = conn.execute(select(requests.c.id).where(requests.c.provider_thread_id == thread["id"])
                                       .order_by(requests.c.number.desc()).limit(1)).scalar_one()
    create(conn, "activity", assignment_id=execution["assignment_id"], execution_id=execution["id"],
           provider_thread_id=thread["id"], provider_request_id=activity_request, usage_record_id=usage["id"],
           kind="progress", occurred_at=observed)
    return True
