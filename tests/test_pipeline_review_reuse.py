from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from archon_horizon.pipeline.connectors import ConnectorFailure, ConnectorManager, ForgejoClient
from archon_horizon.pipeline.errors import DomainError
from archon_horizon.pipeline.records import change, create, get, object_ref
from archon_horizon.pipeline.reviewer_invocations import prepare
from archon_horizon.pipeline.reviews import postprocessing_review_panel, queue_merge, queue_review
from archon_horizon.pipeline.schema import tables
from test_pipeline_reviewer_invocations import review  # noqa: F401
from test_pipeline_service import service_database, world  # noqa: F401


@pytest.fixture
def reuse(world, review):
    change(world.conn, "forge_item", review["item"]["id"], review_phase="postprocessing")
    change(world.conn, "review_policy", world.policy["id"], phases=["postprocessing"])
    prepared = prepare(world.conn, review["actor"], review["data"], world.service)
    request = change(world.conn, "provider_request", prepared["provider_request_id"], status="completed")
    source = create(world.conn, "forge_review", forge_item_id=review["item"]["id"], remote_id="source",
        reviewer_remote_id="specialist", reviewer_descriptor_id=review["descriptor"]["id"],
        reviewer_descriptor_revision_id=request["reviewer_descriptor_revision_id"], provider_request_id=request["id"],
        verdict="approved", summary="Checked reusable definitions and statement assumptions.",
        commit_oid="a" * 40, observed_at=datetime.now(timezone.utc))
    item = change(world.conn, "forge_item", review["item"]["id"], head_commit_oid="b" * 40)
    evidence = dict(kind="review_carry_forward", version=1, forge_item_id=str(item["id"]),
        source_review_id=str(source["id"]), source_commit_oid="a" * 40, head_commit_oid="b" * 40,
        target_branch="main", base_commit_oid="c" * 40, scope_paths=["Metric.lean"],
        delta_analysis="Compared a..b: only the proof of dist_self changed; public declarations are byte-identical.",
        dependency_analysis="Imports, exported assumptions, dependency pins and transitive definition bodies are unchanged.",
        rationale="Statement-design coverage remains applicable; proof checking is reviewed separately.")
    return dict(source=source, evidence=evidence, item=item)


def queued(world, review, reuse, *, evidence=None):
    artifact = create(world.conn, "artifact", project_id=world.project["id"], kind="blob",
        content=world.service.store.put_json(evidence or reuse["evidence"]))
    return queue_review(world.conn, review["actor"], world.service, world.scheduler,
        dict(forge_item_id=str(reuse["item"]["id"]), commit_oid="b" * 40, verdict="approved",
             summary="The changed proof was inspected; existing design evidence remains applicable.",
             carry_forward=[dict(source_review_id=str(reuse["source"]["id"]), evidence_artifact_id=str(artifact["id"]))]),
        str(uuid4()))


def delivered(world, review, reuse, operation):
    maintainer = create(world.conn, "forge_review", forge_item_id=reuse["item"]["id"], remote_id=str(uuid4()),
        reviewer_remote_id="maintainer", verdict="approved", summary=operation["payload"]["summary"],
        commit_oid="b" * 40, observed_at=datetime.now(timezone.utc))
    ConnectorManager._activate_gate(world.conn, operation,
        dict(item=reuse["item"], actor=review["actor"], repository=get(world.conn, "repository", reuse["item"]["repository_id"])),
        maintainer)
    change(world.conn, "outbox_operation", operation["id"], status="completed",
           result_ref_id=object_ref(world.conn, "forge_review", maintainer["id"]))
    return world.conn.execute(select(tables["review_gate"])).mappings().one()


def test_reuse_counts_only_delivered_maintainer_attestation_and_pins_merge_base(world, review, reuse):
    operation = queued(world, review, reuse)
    policy = get(world.conn, "review_policy", world.policy["id"])
    assert postprocessing_review_panel(world.conn, reuse["item"], policy)["missing"] == ["source-review"]
    assert "not fresh specialist approvals" in operation["payload"]["summary"]
    assert "source-review" in operation["payload"]["summary"]
    assert not operation["payload"].get("reviewer_descriptor_id")
    gate = delivered(world, review, reuse, operation)
    panel = postprocessing_review_panel(world.conn, reuse["item"], policy)
    assert panel["missing"] == []
    assert panel["carried_forward"] == ["source-review"]
    merge = queue_merge(world.conn, review["actor"], dict(forge_item_id=str(reuse["item"]["id"]),
        review_gate_id=str(gate["id"]), expected_head_oid="b" * 40), "merge-carry")
    assert merge["payload"]["expected_base_oid"] == "c" * 40


@pytest.mark.parametrize("field,value", [("forge_item_id", str(uuid4())), ("source_review_id", str(uuid4())),
    ("source_commit_oid", "d" * 40), ("head_commit_oid", "d" * 40), ("target_branch", "other"),
    ("delta_analysis", " "), ("dependency_analysis", " "), ("rationale", " ")])
def test_reuse_rejects_mismatched_or_empty_evidence(world, review, reuse, field, value):
    with pytest.raises(DomainError):
        queued(world, review, reuse, evidence={**reuse["evidence"], field: value})


@pytest.mark.parametrize("changed", ["policy", "disabled_policy", "rubric", "guidance", "invocation", "later_objection", "same_head_objection"])
def test_reuse_revalidates_before_merge_even_with_new_current_head_coverage(world, review, reuse, changed):
    operation = queued(world, review, reuse)
    gate = delivered(world, review, reuse, operation)
    if changed == "policy":
        change(world.conn, "review_policy", world.policy["id"], instructions="Updated acceptance requirements")
    elif changed == "disabled_policy":
        change(world.conn, "review_policy", world.policy["id"], enabled=False)
    elif changed == "rubric":
        change(world.conn, "reviewer_descriptor", review["descriptor"]["id"], instructions="Additional assumption audit")
    elif changed == "guidance":
        change(world.conn, "document", world.document["id"], source_commit_oid="d" * 40)
    elif changed == "invocation":
        change(world.conn, "provider_request", reuse["source"]["provider_request_id"], status="interrupted")
    else:
        create(world.conn, "forge_review", forge_item_id=reuse["item"]["id"], remote_id="objection",
            reviewer_remote_id="specialist", reviewer_descriptor_id=review["descriptor"]["id"],
            verdict="changes_requested", summary="The supposedly unchanged definition hides an assumption.",
            commit_oid=("a" if changed == "same_head_objection" else "b") * 40, observed_at=datetime.now(timezone.utc))
    policy = get(world.conn, "review_policy", world.policy["id"])
    assert postprocessing_review_panel(world.conn, reuse["item"], policy)["invalid_carry_forward"]
    with pytest.raises(DomainError, match="Post-processing merge"):
        queue_merge(world.conn, review["actor"], dict(forge_item_id=str(reuse["item"]["id"]),
            review_gate_id=str(gate["id"]), expected_head_oid="b" * 40), "merge-stale")


@pytest.mark.parametrize("method", ["review", "merge"])
def test_remote_base_change_prevents_carry_review_and_merge(method):
    calls = []
    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/user"):
            return httpx.Response(200, json={"id": 1})
        if request.url.path.endswith("/reviews"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"state": "open", "head": {"sha": "b" * 40},
            "base": {"ref": "main", "sha": "d" * 40}, "merged": False})
    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        remote = ForgejoClient("https://forge.invalid", "secret", client=transport)
        with pytest.raises(ConnectorFailure, match="review_carry_base_changed"):
            if method == "review":
                remote.submit_review("owner/repo", 1, expected_head="b" * 40, expected_base_oid="c" * 40,
                    body="Maintainer assessment", verdict="approved", operation_id="reuse")
            else:
                remote.merge_checked("owner/repo", 1, expected_head="b" * 40, expected_base_oid="c" * 40,
                    required_checks=[])
    assert all(call.method == "GET" for call in calls)


def test_lost_review_ack_reconciles_changed_base_without_reposting():
    calls = []
    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/user"):
            return httpx.Response(200, json={"id": 1})
        if request.url.path.endswith("/reviews"):
            return httpx.Response(200, json=[{"id": 10, "user": {"id": 1}, "state": "APPROVED",
                "commit_id": "b" * 40, "body": "<!-- horizon-operation:reuse -->"}])
        return httpx.Response(200, json={"state": "open", "head": {"sha": "b" * 40},
            "base": {"ref": "main", "sha": "d" * 40}, "merged": False})
    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        remote = ForgejoClient("https://forge.invalid", "secret", client=transport)
        receipt = remote.submit_review("owner/repo", 1, expected_head="b" * 40, expected_base_oid="c" * 40,
            body="Maintainer assessment", verdict="approved", operation_id="reuse", reconcile_only=True)
    assert receipt["id"] == 10 and receipt["_horizon_base_oid"] == "d" * 40
    assert all(call.method == "GET" for call in calls)


@pytest.mark.parametrize("changed", ["base", "policy", "rubric"])
def test_reconciled_receipt_preserved_without_stale_gate(world, review, reuse, changed):
    operation = queued(world, review, reuse)
    if changed == "policy":
        change(world.conn, "review_policy", world.policy["id"], enabled=False)
    elif changed == "rubric":
        change(world.conn, "reviewer_descriptor", review["descriptor"]["id"], instructions="Updated rubric")
    remote = {"id": 10, "user": {"id": "maintainer"}, "state": "APPROVED", "commit_id": "b" * 40,
              "_horizon_base_oid": ("d" if changed == "base" else "c") * 40}
    manager = ConnectorManager(None, world.service, lambda _: {})
    reference = manager._record_review(world.conn, operation, dict(item=reuse["item"], actor=review["actor"],
        repository=get(world.conn, "repository", reuse["item"]["repository_id"])), remote)
    assert get(world.conn, "object_reference", reference)["kind"] == "forge_review"
    assert not world.conn.execute(select(tables["review_gate"])).first()


def test_evidence_from_another_project_is_rejected(world, review, reuse):
    project = create(world.conn, "project", slug="other-project", title="Other project")
    artifact = create(world.conn, "artifact", project_id=project["id"], kind="blob",
                      content=world.service.store.put_json(reuse["evidence"]))
    with pytest.raises(DomainError):
        queue_review(world.conn, review["actor"], world.service, world.scheduler,
            dict(forge_item_id=str(reuse["item"]["id"]), commit_oid="b" * 40, verdict="approved",
                 summary="Attestation", carry_forward=[dict(source_review_id=str(reuse["source"]["id"]),
                    evidence_artifact_id=str(artifact["id"]))]), "wrong-project")
