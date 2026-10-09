from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from archon_horizon.pipeline.errors import DomainError
from archon_horizon.pipeline.providers.provider_events import child_state_ref, normalize, project_observation, select_native_event
from archon_horizon.pipeline.persistence.records import create, get, snapshot, change
from archon_horizon.pipeline.persistence.schema import tables
from test_pipeline_service import service_database, world  # noqa: F401


def start(world, adapter="codex"):
    if adapter != "codex":
        changed = change(world.conn, "harness", world.harness["id"], adapter=adapter + "_exec")
        snapshot(world.conn, "harness", changed, world.actor.id)
    run = world.run()
    world.disable_automations(run)
    world.assignment(run)
    grant = world.claim()
    execution = get(world.conn, "execution", grant["execution_id"])
    thread = get(world.conn, "provider_thread", grant["provider_thread_record_id"])
    request = create(world.conn, "provider_request", provider_thread_id=thread["id"], execution_id=execution["id"],
                     number=1, reason="assignment", status="running")
    return execution, thread, request


def observe(world, state, raw, adapter="codex", operation_id=None, observed=None):
    execution, thread, request = state
    return project_observation(world.conn, world.host_actor, execution, thread, request["id"], adapter, raw,
                               operation_id or uuid4(), observed or datetime.now(timezone.utc), world.project["id"])


def total(world, field):
    return world.conn.execute(select(func.sum(tables["usage_record"].c[field]))).scalar()


def test_native_filter_removes_answers_prompts_tool_output_and_invalid_counts():
    raw = {"type": "assistant", "parent_tool_use_id": "child", "message": {"id": "message", "usage": {
        "input_tokens": True, "output_tokens": -4, "cache_read_input_tokens": "20"}, "content": [
            {"type": "text", "text": "private answer"}, {"type": "tool_use", "id": "nested", "name": "Agent",
             "input": {"prompt": "private prompt", "description": "Review", "model": "model"}}]}}
    selected = select_native_event("claude", raw)
    assert "private" not in str(selected)
    assert selected == select_native_event("claude", selected)
    samples, children = normalize("claude", selected, "event")
    assert samples[0].values == dict.fromkeys(("input_tokens", "cached_input_tokens", "output_tokens", "cost_usd"))
    assert {child.key for child in children} == {"child", "nested"}
    assert select_native_event("codex", {"type": "event_msg", "payload": {"type": "token_count"}}) is None


def test_codex_spawn_prompt_retains_only_one_canonical_prepared_review_label():
    request_id = uuid4()
    label = "hz_review_" + request_id.hex
    raw = {"type": "item.completed", "item": {"id": "spawn", "type": "collab_tool_call", "tool": "spawn_agent",
        "sender_thread_id": "parent", "receiver_thread_ids": ["native-child"], "agents_states": {}, "status": "completed",
        "prompt": f"Private instructions\n\nReview label: {label}\n\nPrivate mathematical context"}}
    selected = select_native_event("codex", raw)
    assert selected["item"]["task_name"] == label
    assert "Private" not in str(selected) and "prompt" not in selected["item"]
    assert select_native_event("codex", selected) == selected
    _, children = normalize("codex", selected, "event")
    assert len(children) == 1 and children[0].invocation_ref == request_id

    for prompt in (f"Mention {label} inline", f"Review label: {label}\nReview label: hz_review_{uuid4().hex}",
                   f"Review label: {label}\nReview label: {label}", f"Review label: {label} extra text"):
        assert "task_name" not in select_native_event("codex", {**raw, "item": {**raw["item"], "prompt": prompt}})["item"]
    explicit = "hz_review_" + uuid4().hex
    selected = select_native_event("codex", {**raw, "item": {**raw["item"], "task_name": explicit}})
    assert selected["item"]["task_name"] == explicit


def test_codex_turn_duplicates_and_unknown_values(world):
    state = start(world)
    raw = {"type": "turn.completed", "turn_id": "native-turn", "usage": {"input_tokens": 12, "output_tokens": 0}}
    assert observe(world, state, raw)["usage_records"] == 1
    assert observe(world, state, raw)["usage_records"] == 0
    row = world.conn.execute(select(tables["usage_record"])).mappings().one()
    assert row["input_tokens"] is None
    assert row["output_tokens"] is None
    assert row["accounting"]["raw"]["input_tokens"] == 12
    assert row["accounting"]["status"] == "unknown_scope"
    assert row["cached_input_tokens"] is None
    assert row["cost_usd"] is None
    with pytest.raises(DomainError, match="pinned adapter"):
        observe(world, state, raw, adapter="claude")


def counter(identity, incoming, outgoing, cached=None, *, epoch="session"):
    return {"type": "horizon.usage_snapshot", "usage": {
        "input_tokens": incoming, "output_tokens": outgoing, "cached_input_tokens": cached},
        "accounting": {"scope": "native_thread_cumulative", "identity": identity, "epoch": epoch,
                       "source": "rollout_total_token_usage", "provider_version": "0.153.4"}}


def test_native_session_counters_resume_across_horizon_rows_and_keep_failed_usage(world):
    execution, thread, request = state = start(world)
    native = str(uuid4())
    at = datetime.now(timezone.utc)
    first = counter(native, 100, 10, 70)
    assert observe(world, state, first, observed=at)["usage_records"] == 1
    assert observe(world, state, first, observed=at)["usage_records"] == 0
    change(world.conn, "provider_thread", thread["id"], status="closed")
    recovered = create(world.conn, "provider_thread", assignment_id=thread["assignment_id"],
        number=2, kind="primary", workspace_id=thread["workspace_id"], harness_revision_id=thread["harness_revision_id"],
        skill_bundle_artifact_id=thread["skill_bundle_artifact_id"], provider_thread_id=native,
        provider_state_ref="recovered-native:" + native,
        applied_model_options={}, status="available")
    failed = create(world.conn, "provider_request", provider_thread_id=recovered["id"], execution_id=execution["id"],
                    number=1, reason="continuation", status="failed")
    resumed = execution, recovered, failed
    observe(world, resumed, counter(native, 130, 15, 90), observed=at + timedelta(seconds=10))
    assert total(world, "input_tokens") == 130
    assert total(world, "cached_input_tokens") == 90  # Included in input, never added to it.
    assert total(world, "output_tokens") == 15
    assert total(world, "cost_usd") is None
    measured = world.conn.execute(select(tables["usage_record"]).where(
        tables["usage_record"].c.provider_thread_id == recovered["id"])).mappings().one()
    assert measured["input_tokens"] == 30
    assert get(world.conn, "provider_request", failed["id"])["status"] == "failed"


def test_counter_order_reset_epoch_and_missing_fields_remain_explicit(world):
    state = start(world)
    native, at = str(uuid4()), datetime.now(timezone.utc)
    observe(world, state, counter(native, 100, 10), observed=at)
    observe(world, state, counter(native, 130, 15), observed=at + timedelta(seconds=10))
    observe(world, state, counter(native, 110, 12), observed=at + timedelta(seconds=5))
    assert total(world, "input_tokens") == 130
    observe(world, state, counter(native, 5, 1), observed=at + timedelta(seconds=20))
    observe(world, state, counter(native, 150, 20), observed=at + timedelta(seconds=30))
    assert total(world, "input_tokens") == 130  # An unexplained reset is not free, nor a new epoch.
    statuses = list(world.conn.execute(select(tables["usage_record"].c.accounting)).scalars())
    assert [row["status"] for row in statuses][-3:] == ["out_of_order", "counter_reset_unknown", "counter_reset_unknown"]
    observe(world, state, counter(native, 20, 3, epoch="explicit-new-epoch"), observed=at + timedelta(seconds=40))
    assert total(world, "input_tokens") == 150
    observe(world, state, counter(native, 30, 4, 8, epoch="explicit-new-epoch"), observed=at + timedelta(seconds=50))
    assert total(world, "cached_input_tokens") == 8
    assert total(world, "input_tokens") == 160


def test_rollout_and_known_stdout_totals_share_checkpoint(world):
    state, native, at = start(world), str(uuid4()), datetime.now(timezone.utc)
    raw = counter(native, 120, 12, 100)
    observe(world, state, raw, observed=at)
    raw["type"] = "turn.completed"
    raw["accounting"]["source"] = "rollout_correlated_completion"
    observe(world, state, raw, observed=at + timedelta(seconds=1))
    assert total(world, "input_tokens") == 120
    assert total(world, "output_tokens") == 12


def test_late_counter_revealing_an_earlier_reset_does_not_restore_false_certainty(world):
    from archon_horizon.pipeline.providers.usage_accounting import uncertain_usage

    state, native, at = start(world), str(uuid4()), datetime.now(timezone.utc)
    observe(world, state, counter(native, 100, 10), observed=at + timedelta(seconds=10))
    observe(world, state, counter(native, 200, 20), observed=at)
    observe(world, state, counter(native, 250, 25), observed=at + timedelta(seconds=20))
    assert total(world, "input_tokens") == 100
    usage = tables["usage_record"]
    assert world.conn.execute(select(func.bool_or(uncertain_usage(usage)))).scalar_one() is True


def test_generic_native_child_is_observed_without_review_authority(world):
    state = start(world)
    raw = {"type": "horizon.child_notifications", "children": [
        {"key": "/root/consumer_probe", "status": "running", "discovered": True, "invocation_id": "spawn"}]}
    observe(world, state, raw)
    raw["children"][0]["status"] = "completed"
    observe(world, state, raw)
    child = world.conn.execute(select(tables["provider_thread"]).where(
        tables["provider_thread"].c.kind == "child")).mappings().one()
    assert child["label"] == "consumer_probe"
    requests = tables["provider_request"]
    request = world.conn.execute(select(requests).where(requests.c.provider_thread_id == child["id"])).mappings().one()
    assert request["reviewer_descriptor_id"] is None
    assert request["status"] == "completed"
    raw["children"][0].update(status="running", invocation_id="followup")
    observe(world, state, raw)
    assert world.conn.execute(select(func.count()).select_from(requests).where(
        requests.c.provider_thread_id == child["id"])).scalar_one() == 2


def test_stdout_native_id_and_rollout_task_name_describe_one_generic_child(world):
    state, native = start(world), str(uuid4())
    observe(world, state, {"type": "item.completed", "item": {"id": "spawn", "type": "collab_tool_call",
        "tool": "spawn_agent", "receiver_thread_ids": [native]}})
    observe(world, state, {"type": "horizon.child_notifications", "children": [{
        "key": "/root/consumer", "native_id": native, "status": "running", "discovered": True, "invocation_id": "spawn"}]})
    observe(world, state, {"type": "horizon.child_notifications", "children": [{
        "key": "/root/consumer", "status": "completed", "discovered": True}]})
    threads, requests = tables["provider_thread"], tables["provider_request"]
    child = world.conn.execute(select(threads).where(threads.c.kind == "child")).mappings().one()
    request = world.conn.execute(select(requests).where(requests.c.provider_thread_id == child["id"])).mappings().one()
    assert request["status"] == "completed"
    assert request["reviewer_descriptor_id"] is None


def test_historical_totals_are_preserved_but_not_summed_and_authority_can_cover_them(world):
    from archon_horizon.pipeline.providers.usage_accounting import trusted_amount, uncertain_usage

    execution, thread, request = state = start(world)
    native = str(uuid4())
    change(world.conn, "provider_thread", thread["id"], provider_thread_id=native)
    usage = tables["usage_record"]
    old = create(world.conn, "usage_record", execution_id=execution["id"], provider_thread_id=thread["id"],
                 provider_record_id="codex:old-request:counter:", input_tokens=1000, output_tokens=100)
    query = select(func.sum(trusted_amount(usage, "input_tokens")), func.bool_or(uncertain_usage(usage)))
    assert tuple(world.conn.execute(query).one()) == (None, True)
    observe(world, state, counter(native, 1200, 120), observed=datetime.now(timezone.utc) + timedelta(seconds=1))
    assert tuple(world.conn.execute(query).one()) == (1200, False)
    assert get(world.conn, "usage_record", old["id"])["input_tokens"] == 1000
    observe(world, state, counter(native, 2, 1), observed=datetime.now(timezone.utc) + timedelta(seconds=2))
    assert tuple(world.conn.execute(query).one()) == (1200, True)


def test_claude_message_snapshots_delta_but_result_aggregate_is_not_added(world):
    state = start(world, "claude")
    message = {"type": "assistant", "message": {"id": "message", "usage": {
        "input_tokens": 10, "cache_read_input_tokens": 20, "cache_creation_input_tokens": 5, "output_tokens": 2}}}
    assert observe(world, state, message, "claude")["usage_records"] == 1
    assert observe(world, state, message, "claude")["usage_records"] == 0
    message["message"]["usage"]["output_tokens"] = 9
    assert observe(world, state, message, "claude")["usage_records"] == 1
    observe(world, state, {"type": "result", "usage": {"input_tokens": 200, "output_tokens": 60}, "total_cost_usd": 0.3}, "claude")
    assert total(world, "input_tokens") == 35
    assert total(world, "cached_input_tokens") == 20
    assert total(world, "output_tokens") == 9
    assert total(world, "cost_usd") == Decimal("0.3")
    observe(world, state, {"type": "result", "usage": {}, "total_cost_usd": 0.5}, "claude")
    assert total(world, "cost_usd") == Decimal("0.5")


def test_codex_child_mirrored_once_and_wait_snapshot_does_not_resurrect(world):
    state = start(world)
    spawn = {"type": "item.completed", "item": {"id": "spawn-call", "type": "collab_tool_call", "tool": "spawn_agent",
        "receiver_thread_ids": ["native-child"], "agents_states": {"native-child": {"status": "running"}}}}
    assert observe(world, state, spawn)["child_changes"] == 1
    assert observe(world, state, spawn)["child_changes"] == 0
    completed = {"type": "item.completed", "item": {"id": "wait-call", "type": "collab_tool_call", "tool": "wait",
        "agents_states": {"native-child": {"status": "completed", "usage": {"input_tokens": 30, "output_tokens": 12}}}}}
    assert observe(world, state, completed) == {"usage_records": 1, "child_changes": 1}
    assert observe(world, state, completed) == {"usage_records": 0, "child_changes": 0}
    observe(world, state, spawn)
    child = world.conn.execute(select(tables["provider_thread"]).where(tables["provider_thread"].c.kind == "child")).mappings().one()
    request = world.conn.execute(select(tables["provider_request"]).where(tables["provider_request"].c.provider_thread_id == child["id"])).mappings().one()
    assert request["status"] == "completed"
    assert request["reviewer_descriptor_id"] is None
    assert child["parent_request_id"] == state[2]["id"]
    assert total(world, "input_tokens") == 30
    assert world.conn.execute(select(func.count()).select_from(tables["execution"])).scalar_one() == 1


def test_claude_children_usage_remains_separate_and_tool_results_only_close_known_children(world):
    state = start(world, "claude")
    observe(world, state, {"type": "assistant", "message": {"id": "parent", "usage": {"input_tokens": 10},
        "content": [{"type": "tool_use", "name": "Agent", "id": "child-call", "input": {"description": "Reviewer"}}]}}, "claude")
    observe(world, state, {"type": "assistant", "parent_tool_use_id": "child-call", "message": {
        "id": "child-message", "usage": {"input_tokens": 50, "output_tokens": 20}}}, "claude")
    observe(world, state, {"type": "result", "usage": {"input_tokens": 60, "output_tokens": 20}, "total_cost_usd": 0.5}, "claude")
    observe(world, state, {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "shell-tool"}, {"type": "tool_result", "tool_use_id": "child-call"}]}}, "claude")
    children = list(world.conn.execute(select(tables["provider_thread"]).where(tables["provider_thread"].c.kind == "child")).mappings())
    assert len(children) == 1
    assert children[0]["provider_thread_id"] is None
    assert children[0]["description"] == "Reviewer"
    assert total(world, "input_tokens") == 60
    assert total(world, "cost_usd") is None  # Inclusive aggregate is retained as evidence, never double billed.
    child_request = world.conn.execute(select(tables["provider_request"]).where(
        tables["provider_request"].c.provider_thread_id == children[0]["id"])).mappings().one()
    assert child_request["status"] == "completed"


def test_nested_claude_child_has_actual_parent_request(world):
    state = start(world, "claude")
    observe(world, state, {"type": "assistant", "parent_tool_use_id": "parent-child", "message": {
        "id": "message", "usage": {"input_tokens": 0}, "content": [
            {"type": "tool_use", "name": "Agent", "id": "nested-child", "input": {"description": "Nested"}}]}}, "claude")
    children = list(world.conn.execute(select(tables["provider_thread"]).where(tables["provider_thread"].c.kind == "child")
                                      .order_by(tables["provider_thread"].c.number)).mappings())
    assert len(children) == 2
    requests = tables["provider_request"]
    parent_request_id = world.conn.execute(select(requests.c.id).where(requests.c.provider_thread_id == children[0]["id"])).scalar_one()
    assert children[1]["parent_request_id"] == parent_request_id
    assert total(world, "input_tokens") == 0
    assert total(world, "output_tokens") is None
    activity = tables["activity"]
    measured = world.conn.execute(select(activity).where(activity.c.usage_record_id.is_not(None))).mappings().one()
    assert measured["provider_request_id"] == parent_request_id


def test_old_spawn_does_not_create_another_request_after_child_followup(world):
    state = start(world)
    spawn = {"type": "item.completed", "item": {"id": "first", "type": "collab_tool_call", "tool": "spawn_agent",
        "receiver_thread_ids": ["child"]}}
    done = {"type": "item.completed", "item": {"type": "collab_tool_call", "tool": "wait",
        "agents_states": {"child": {"status": "completed"}}}}
    observe(world, state, spawn)
    observe(world, state, done)
    followup = {"type": "item.completed", "item": {"id": "second", "type": "collab_tool_call", "tool": "send_input",
        "receiver_thread_ids": ["child"], "agents_states": {"child": {"status": "running"}}}}
    observe(world, state, followup)
    observe(world, state, done)
    observe(world, state, spawn)
    requests = tables["provider_request"]
    child_requests = list(world.conn.execute(select(requests).where(requests.c.id != state[2]["id"])).mappings())
    assert len(child_requests) == 2
    assert {row["status"] for row in child_requests} == {"completed"}


def test_registered_child_reviewer_pins_survive_native_observations(world):
    execution, parent, request = state = start(world)
    descriptor = create(world.conn, "reviewer_descriptor", project_id=world.project["id"], slug="test-reviewer",
                        functions=["statement_alignment"], instructions="Check statements")
    revision = snapshot(world.conn, "reviewer_descriptor", descriptor, world.actor.id)
    child = create(world.conn, "provider_thread", assignment_id=parent["assignment_id"], number=2, kind="child",
        parent_request_id=request["id"], workspace_id=parent["workspace_id"], harness_revision_id=parent["harness_revision_id"],
        skill_bundle_artifact_id=parent["skill_bundle_artifact_id"], provider_thread_id="review-child",
        provider_state_ref=child_state_ref(parent["id"], "codex", "review-child"), applied_model_options={}, status="available")
    registered = create(world.conn, "provider_request", provider_thread_id=child["id"], execution_id=execution["id"],
        number=1, reason="review", reviewer_descriptor_id=descriptor["id"], reviewer_descriptor_revision_id=revision,
        guidance_manifest_artifact_id=parent["skill_bundle_artifact_id"], status="pending")
    observe(world, state, {"type": "item.completed", "item": {"id": "call", "type": "collab_tool_call", "tool": "spawn_agent",
        "receiver_thread_ids": ["review-child"]}})
    running = get(world.conn, "provider_request", registered["id"])
    assert running["status"] == "running"
    assert running["reviewer_descriptor_id"] == descriptor["id"]
    assert running["reviewer_descriptor_revision_id"] == revision
    assert running["guidance_manifest_artifact_id"] == parent["skill_bundle_artifact_id"]


def test_background_child_launch_ack_does_not_complete_delegated_work(world):
    state = start(world, "claude")
    observe(world, state, {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Agent", "id": "background-child", "input": {
            "description": "Review", "run_in_background": True}}]}}, "claude")
    observe(world, state, {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "background-child"}]}}, "claude")
    requests, threads = tables["provider_request"], tables["provider_thread"]
    query = select(requests).join(threads, requests.c.provider_thread_id == threads.c.id).where(threads.c.kind == "child")
    request = world.conn.execute(query).mappings().one()
    assert request["status"] == "running" and request["native_background"] is True
    observe(world, state, {"type": "queue-operation", "operation": "enqueue", "content":
        '<task-notification><tool-use-id>background-child</tool-use-id><status>completed</status>'
        '<summary>Agent "Review" finished</summary></task-notification>'}, "claude")
    assert world.conn.execute(query).mappings().one()["status"] == "completed"
