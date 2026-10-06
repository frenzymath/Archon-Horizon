from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from archon_horizon.pipeline.models import (
    AssignmentCreate, Condition, GitTarget, IntegrationCreate, ModelOptions,
    ObjectRef, ObligationResolve, RetryPolicy, RunCreate, RunPhase, SandboxPolicy,
    validate_operation,
)


def test_closed_discriminated_references_and_phases() -> None:
    ref = TypeAdapter(ObjectRef)
    assert ref.validate_python({"kind": "file", "repository_id": uuid4(), "path": "Math/Proof.lean"}).path == "Math/Proof.lean"
    for path in ("../secret", "/etc/passwd", "Math/../secret", "Math//Proof", "Math\\secret", "."):
        with pytest.raises(ValidationError):
            ref.validate_python({"kind": "file", "repository_id": uuid4(), "path": path})
    with pytest.raises(ValidationError):
        ref.validate_python({"kind": "mission", "id": uuid4(), "path": "extra"})
    with pytest.raises(ValidationError):
        TypeAdapter(RunPhase).validate_python({"kind": "formalization", "roadmap_document_id": uuid4()})


def test_conditions_bound_shape_and_status_vocabulary() -> None:
    expr = {"op": "queue_below", "run_id": uuid4(), "count": 2}
    assert Condition(expression=expr).expression.count == 2
    for invalid in (
        {"op": "queue_below", "run_id": uuid4(), "count": "2"},
        {"op": "queue_below", "run_id": uuid4(), "count": True},
        {"op": "all", "args": []},
        {"op": "sql", "query": "SELECT 1"},
        {"op": "status_in", "target": {"kind": "host", "id": uuid4()}, "values": ["enabled"]},
        {"op": "status_in", "target": {"kind": "mission", "id": uuid4()}, "values": ["failed"]},
    ):
        with pytest.raises(ValidationError):
            Condition(expression=invalid)
    workspace_ready = Condition(expression={"op": "status_in", "target": {"kind": "workspace", "id": uuid4()},
                                           "values": ["ready"]})
    assert workspace_ready.expression.values == ["ready"]
    nested = expr
    for _ in range(8):
        nested = {"op": "not", "arg": nested}
    with pytest.raises(ValidationError, match="depth 8"):
        Condition(expression=nested)
    with pytest.raises(ValidationError):
        Condition(expression={"op": "all", "args": [expr] * 128})


def test_timestamps_are_aware_normalized_and_start_windows_are_ordered() -> None:
    fields = {"run_id": uuid4(), "mission_id": uuid4()}
    with pytest.raises(ValidationError):
        AssignmentCreate(**fields, not_before=datetime(2026, 1, 1))
    with pytest.raises(ValidationError):
        AssignmentCreate(**fields, not_before=12345678)
    start = datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=8)))
    assignment = AssignmentCreate(**fields, not_before=start)
    assert assignment.not_before.utcoffset() == timedelta(0)
    with pytest.raises(ValidationError, match="precede"):
        AssignmentCreate(**fields, not_before=start, expires_at=start)


def test_retry_policy_and_inherited_model_options() -> None:
    assert ModelOptions().model_dump(exclude_none=True) == {}
    with pytest.raises(ValidationError):
        ModelOptions(model=None)
    with pytest.raises(ValidationError):
        ModelOptions(extra_env={"HOME": "/"})
    with pytest.raises(ValidationError):
        RetryPolicy(initial_delay_seconds=20, max_delay_seconds=5)


def test_run_assignment_budget_remains_an_operator_choice() -> None:
    run = RunCreate(mission_id=uuid4(), phase={"kind": "preprocessing", "roadmap_document_id": uuid4()}, host_ids=[uuid4()])
    assert run.max_assignments is None
    assert RunCreate(mission_id=uuid4(), phase={"kind": "preprocessing", "roadmap_document_id": uuid4()},
                     host_ids=[uuid4()], max_assignments=None).max_assignments is None


def test_handled_work_requires_a_concrete_resolution() -> None:
    with pytest.raises(ValidationError):
        ObligationResolve(expected_revision=1, status="handled", resolution={"kind": "delegated", "note": "done", "assignment_ids": []})
    with pytest.raises(ValidationError):
        ObligationResolve(expected_revision=1, status="done", resolution={"kind": "scheduled", "note": "later", "assignment_id": uuid4()})
    result = ObligationResolve(expected_revision=1, status="handled", resolution={"kind": "scheduled", "note": "owned follow-up", "assignment_id": uuid4()})
    assert result.resolution.kind == "scheduled"


def test_operation_payloads_are_closed_and_versioned() -> None:
    payload = {"publication_id": str(uuid4())}
    assert validate_operation("publication", 1, payload).publication_id
    with pytest.raises(ValueError):
        validate_operation("publication", 2, payload)
    with pytest.raises(ValidationError):
        validate_operation("publication", 1, {**payload, "shell": "arbitrary command"})
    merge = validate_operation("forge_merge", 1, {"forge_item_id": uuid4(), "review_gate_id": uuid4(), "expected_head_oid": "a" * 40})
    assert merge.expected_head_oid == "a" * 40
    with pytest.raises(ValidationError):
        validate_operation("forge_merge", 1, {"forge_item_id": uuid4(), "review_gate_id": uuid4()})
    with pytest.raises(ValidationError):
        validate_operation("forge_review", 1, {"forge_item_id": uuid4(), "commit_oid": "a" * 40, "verdict": "merge", "summary": "No"})
    create = {"repository_id": uuid4(), "origin_run_id": uuid4(), "review_phase": "formalization",
              "kind": "pull_request", "title": "Proof", "body": "Review this proof", "head": "proof", "base": "main"}
    assert validate_operation("forge_create", 1, create).head == "proof"
    assert validate_operation("forge_create", 1, {**create, "base": None}).base is None
    with pytest.raises(ValidationError):
        validate_operation("forge_create", 1, {**create, "head": None})
    with pytest.raises(ValidationError):
        validate_operation("forge_create", 1, {**create, "kind": "issue"})


def test_sandbox_requires_pinned_image_and_integrations_reject_plaintext_remote() -> None:
    with pytest.raises(ValidationError):
        SandboxPolicy(image_digest="lean:latest")
    assert SandboxPolicy(image_digest="registry/lean@sha256:" + "a" * 64).mode == "rootless_container"
    assert SandboxPolicy(mode="unrestricted").image_digest is None
    with pytest.raises(ValidationError):
        IntegrationCreate(kind="forge", endpoint="http://example.org", credential_ref="secret:forge")
    assert IntegrationCreate(kind="forge", endpoint="http://127.0.0.1:3000", credential_ref="secret:forge")


def test_forge_changes_pin_base_and_blob_preconditions_without_branch_override():
    base = {"repository_id": uuid4(), "origin_run_id": uuid4(), "base_commit_oid": "a" * 40, "message": "Refine statements"}
    create_file = {"operation": "create", "path": "Math/New.lean", "content_artifact_id": uuid4()}
    update_file = {"operation": "update", "path": "Math/Existing.lean", "content_artifact_id": uuid4(), "sha": "b" * 40}
    delete_file = {"operation": "delete", "path": "Math/Obsolete.lean", "sha": "c" * 64}
    valid = {**base, "files": [create_file, update_file, delete_file]}
    parsed = validate_operation("forge_change", 1, valid)
    assert [file.operation for file in parsed.files] == ["create", "update", "delete"]
    assert validate_operation("forge_change", 1, {**valid, "base_commit_oid": "a" * 64}).base_commit_oid
    for invalid in (
        {**valid, "branch": "main"}, {**valid, "integration_identity_id": uuid4()}, {**valid, "id": uuid4()},
        {**valid, "base_commit_oid": "main"}, {**valid, "base_commit_oid": "A" * 40},
        {**valid, "files": []}, {**valid, "files": [create_file, create_file]},
        {**valid, "files": [{**create_file, "path": "../outside"}]},
        {**valid, "files": [{**create_file, "content_artifact_id": None}]},
        {**valid, "files": [{**create_file, "sha": "a" * 40}]},
        {**valid, "files": [{**update_file, "sha": None}]},
        {**valid, "files": [{**update_file, "sha": "a" * 41}]},
        {**valid, "files": [{**update_file, "content_artifact_id": None}]},
        {**valid, "files": [{**delete_file, "sha": None}]},
        {**valid, "files": [{**delete_file, "content_artifact_id": uuid4()}]},
        {**valid, "files": [{**create_file, "path": f"Math/File{number}.lean"} for number in range(201)]},
    ):
        with pytest.raises(ValidationError):
            validate_operation("forge_change", 1, invalid)


def test_publication_ref_must_be_a_full_safe_git_ref() -> None:
    for ref_name in ("main", "refs/heads/a..b", "refs/heads/a.lock", "refs/heads/a b", "refs//main"):
        with pytest.raises(ValidationError):
            GitTarget(kind="git", repository_id=uuid4(), ref_name=ref_name)
    assert GitTarget(kind="git", repository_id=uuid4(), ref_name="refs/heads/proofs/topic").ref_name
