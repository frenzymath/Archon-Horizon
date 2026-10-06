from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

import pytest

from archon_horizon.pipeline import cli, readmodels
from archon_horizon.pipeline.errors import DomainError
from archon_horizon.pipeline.records import create, get
from archon_horizon.pipeline.worker.contracts import Operation
from archon_horizon.pipeline.worker.journal import DurableJournal
from archon_horizon.pipeline.worker_events import WorkerOperation, handle
from test_pipeline_service import service_database, world


def test_worker_publication_failure_and_late_verification(world):
    run = world.run()
    world.disable_automations(run)
    world.assignment(run)
    grant = world.claim()
    oid = "a" * 40
    now = datetime.now(timezone.utc)
    payload = {"repository_id": str(world.workspace_repo["id"]), "commit_oid": oid,
               "recovery_ref": "refs/horizon/recovery/" + oid, "failure_code": "git_authentication_failed"}
    def receipt(kind, at=now):
        return handle(world.conn, world.host_actor, WorkerOperation(operation_id=uuid4(),
            execution_id=grant["execution_id"], epoch=grant["epoch"], kind=kind,
            payload=payload, occurred_at=at.timestamp()), world.service, world.scheduler)
    failed = receipt("publication_failed")
    publication = get(world.conn, "publication", failed["publication_id"])
    assert publication["status"] == "failed"
    assert publication["failure"]["code"] == "git_authentication_failed"
    payload["failure_code"] = ["invalid"]
    receipt("publication_failed")
    publication = get(world.conn, "publication", failed["publication_id"])
    assert publication["failure"]["code"] == "publication_blocked"
    with pytest.raises(DomainError) as error:
        world.command("retry_publication", publication)
    assert error.value.code == "worker_repair_required"
    payload["remote_ref"] = "refs/heads/horizon/recovery/" + oid
    receipt("publication_verified")
    receipt("publication_failed", now - timedelta(hours=1))
    verified = get(world.conn, "publication", failed["publication_id"])
    assert verified["status"] == "verified" and verified["failure"] is None
    assert abs((verified["verified_at"] - now).total_seconds()) < 30
    result = readmodels.changes(world.conn, world.actor, world.project["id"], None, 30)
    assert result["preservation"] == {"pending_count": 0, "blocked_count": 0,
                                      "oldest_pending_at": None, "last_verified_at": verified["verified_at"]}
    assert result["items"][0]["worker_managed"] is True


def test_preservation_summary_is_project_scoped_and_independent_of_page_filter(world):
    run = world.run()
    assignment = world.assignment(run)
    stamp = datetime.now(timezone.utc) - timedelta(hours=2)
    artifact = create(world.conn, "artifact", project_id=world.project["id"], kind="commit",
                      content={"repository_id": str(world.workspace_repo["id"]), "commit_oid": "b" * 40})
    create(world.conn, "publication", artifact_id=artifact["id"], requested_by_assignment_id=assignment["id"],
           target={"kind": "git", "repository_id": str(world.workspace_repo["id"]), "ref_name": "refs/horizon/preserved/b"},
           created_at=stamp, status="failed")
    create(world.conn, "publication", artifact_id=artifact["id"], requested_by_assignment_id=assignment["id"],
           target={"kind": "artifact_store", "key": "not-a-workspace-backup"}, status="pending")
    result = readmodels.changes(world.conn, world.actor, world.project["id"], None, 1, q="nomatch")
    assert result["items"] == []
    assert result["preservation"]["pending_count"] == 1
    assert result["preservation"]["blocked_count"] == 1
    assert result["preservation"]["oldest_pending_at"] == stamp


def test_local_publication_inspection_and_repair_preserves_operation_key(tmp_path):
    config = {"host_id": str(uuid4()), "api_url": "http://127.0.0.1:1", "token_file": str(tmp_path / "token"),
              "journal_root": str(tmp_path / "journal"), "workspace_roots": [str(tmp_path / "workspace")],
              "journal_min_free_bytes": 0, "harnesses": [{"id": str(uuid4()), "adapter": "codex_exec", "executable": "codex",
              "provider_home": str(tmp_path / "provider"), "scratch_root": str(tmp_path / "scratch"), "sandbox": {"mode": "unrestricted"}}]}
    path = tmp_path / "worker.json"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="No existing"):
        cli.worker_publications(path)
    journal = DurableJournal(tmp_path / "journal", minimum_free_bytes=0)
    try:
        operation = Operation.create("execution", 1, "publication_discovered", {
            "repository_id": "repository", "commit_oid": "a" * 40, "recovery_ref": "refs/horizon/recovery/a"})
        journal.enqueue(operation, destination="local_git")
        claim = journal.claim(destination="local_git")
        journal.settle(claim, "blocked", error="git_authentication_failed")
        inspected = cli.worker_publications(path)
        assert inspected["pending_count"] == 1
        assert inspected["operations"][0]["state"] == "blocked"
        with pytest.raises(ValueError, match="repair note"):
            cli.worker_publications(path, operation.operation_id)
        result = cli.worker_publications(path, operation.operation_id, "Corrected worker credentials")
        assert result["operations"][0]["operation_id"] == operation.operation_id
        assert result["operations"][0]["state"] == "pending"
    finally:
        journal.close()
