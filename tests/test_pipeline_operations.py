from __future__ import annotations

from datetime import datetime, timedelta, timezone
from contextlib import nullcontext
import json
import hashlib
import logging
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from archon_horizon.pipeline import cli
from archon_horizon.pipeline.operations import storage
from archon_horizon.pipeline.persistence.artifacts import ArtifactStore
from archon_horizon.pipeline.client import AgentClient, RetryDeferred
from archon_horizon.pipeline.config import PipelineConfig, StoragePolicy, load_config
from archon_horizon.pipeline.persistence.records import create
from archon_horizon.pipeline.persistence.schema import tables
from archon_horizon.pipeline.worker_config import WorkerConfig, private_text
from test_pipeline_service import service_database, world


def test_init_is_read_only_until_apply_and_never_overwrites(tmp_path, capsys):
    target = tmp_path / "config" / "server.json"
    root = tmp_path / "state"
    args = ["--config", str(target), "init", "--state-root", str(root),
            "--database-url", "postgresql+psycopg://operator:unprinted-secret@127.0.0.1:1/disposable"]
    cli.main(args)
    assert not target.exists() and not root.exists()
    assert "unprinted-secret" not in capsys.readouterr().out
    cli.main([*args, "--apply"])
    assert target.stat().st_mode & 0o777 == 0o600
    assert (root / "tmp").is_dir()
    before = target.read_bytes()
    with pytest.raises(SystemExit):
        cli.main([*args, "--apply"])
    assert target.read_bytes() == before
    assert load_config(target).state_root == root


def test_config_redaction_and_validation_do_not_expose_database_secrets(tmp_path, capsys):
    config = PipelineConfig(database_url="postgresql+psycopg://a:hidden@127.0.0.1/test?sslpassword=other-hidden", state_root=tmp_path)
    assert "hidden" not in json.dumps(config.redacted())
    with pytest.raises(SystemExit):
        cli.main(["--config", str(tmp_path / "config"), "init", "--state-root", "relative", "--database-url", "SECRET-INVALID-URL"])
    assert "SECRET-INVALID-URL" not in capsys.readouterr().err
    with pytest.raises(ValidationError):
        StoragePolicy(idempotency_retention_seconds=10, max_offline_replay_seconds=10)


def test_doctor_checks_only_explicit_configuration_without_creating_state(tmp_path, monkeypatch):
    missing = tmp_path / "absent"
    config = PipelineConfig(database_url="postgresql+psycopg://a:b@127.0.0.1:1/test", state_root=missing)
    class Offline:
        def __enter__(self):
            raise ConnectionError("secret should never be printed")
        def __exit__(self, *_):
            pass
    monkeypatch.setattr(cli, "database", lambda selected: Offline() if selected is config else pytest.fail("wrong config"))
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503))) as probe:
        result = cli.doctor(config, client=probe)
    assert not missing.exists()
    assert not result["healthy"]
    assert result["checks"][0] == {"name": "database", "status": "unavailable", "detail": "ConnectionError"}
    assert "secret should" not in json.dumps(result)


def test_worker_entry_imports_without_database_or_scientific_dependencies():
    code = """
import importlib.abc, sys
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in {'sqlalchemy', 'psycopg', 'alembic', 'numpy', 'scipy', 'bm25s', 'fastapi'}:
            raise RuntimeError('unexpected worker dependency: ' + fullname)
sys.meta_path.insert(0, Guard())
from archon_horizon.pipeline.worker_config import WorkerConfig, load_worker
from archon_horizon.pipeline.worker.daemon import WorkerDaemon
from archon_horizon.pipeline.client import AgentClient
from archon_horizon.pipeline.cli import parser
parser().parse_args(['worker', '--worker-config', '/explicit/worker.json'])
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr.decode()


def test_interactive_init_prompts_only_missing_fields_and_never_connects(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("HORIZON_PIPELINE_DATABASE_URL", raising=False)
    monkeypatch.setenv("ISOLATED_DATABASE", "postgresql+psycopg://a:private-password@127.0.0.1/test")
    answers = iter([str(tmp_path / "state"), "https://pipeline.example.org", "ISOLATED_DATABASE"])
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or next(answers))
    cli.main(["--config", str(tmp_path / "config.json"), "init", "--interactive"])
    assert len(prompts) == 3
    assert "private-password" not in capsys.readouterr().out
    assert not (tmp_path / "state").exists()
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("asked for an already provided field"))
    cli.main(["--config", str(tmp_path / "config.json"), "init", "--interactive",
              "--state-root", str(tmp_path / "state"), "--public-url", "https://pipeline.example.org",
              "--database-url", os.environ["ISOLATED_DATABASE"]])
    capsys.readouterr()
    with pytest.raises(SystemExit):
        cli.main(["--config", str(tmp_path / "config.json"), "init", "--non-interactive"])
    result = json.loads(capsys.readouterr().err)
    assert result["error"]["code"] == "missing_configuration"
    assert set(result["error"]["fields"]) == {"state_root", "database_url"}


def test_worker_diagnostics_and_claim_reconciliation_are_scoped(tmp_path, monkeypatch):
    import fcntl
    from archon_horizon.pipeline.operations.diagnostics import worker_checks
    from archon_horizon.pipeline.worker_config import open_journal
    host_id, harness_id = uuid4(), uuid4()
    token = tmp_path / "host-token"
    token.write_text("host-private-token")
    token.chmod(0o600)
    values = {"host_id": str(host_id), "api_url": "https://isolated.invalid", "token_file": str(token),
              "journal_root": str(tmp_path / "journal"), "workspace_roots": [str(tmp_path / "workspace")],
              "journal_min_free_bytes": 0,
              "harnesses": [{"id": str(harness_id), "adapter": "codex_exec", "executable": "/missing/codex",
                             "provider_home": str(tmp_path / "provider"), "scratch_root": str(tmp_path / "scratch"),
                             "sandbox": {"mode": "unrestricted"}}]}
    worker = WorkerConfig.model_validate(values)
    config = tmp_path / "worker.json"
    config.write_text(json.dumps(values))
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: pytest.fail("provider was invoked"))
    checks = worker_checks(config)
    assert next(row for row in checks if row["name"] == "worker_provider_binary")["status"] == "missing"
    assert "host-private-token" not in json.dumps(checks)
    assert before == sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    journal = open_journal(worker)
    claim = journal.claim_attempt(str(host_id), [str(harness_id)])
    journal.close()
    remote = {"executions": [{"id": "unconfirmed"}], "has_more": False}
    requests = []
    def respond(request):
        assert request.headers["authorization"] == "Bearer host-private-token"
        assert request.url.path == f"/api/v3/worker/hosts/{host_id}/unconfirmed-executions"
        requests.append(request)
        return httpx.Response(200, json=remote)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(cli.DomainError, match="unconfirmed"):
            cli.reconcile_worker_claim(config, "No process remains", client=client)
        with (worker.journal_root / "daemon.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with pytest.raises(ValueError, match="Stop the configured worker"):
                cli.reconcile_worker_claim(config, "No process remains", client=client)
        assert len(requests) == 1
        remote = {"executions": [], "has_more": True}
        with pytest.raises(cli.DomainError):
            cli.reconcile_worker_claim(config, "No process remains", client=client)
        remote = {"executions": [], "has_more": False}
        result = cli.reconcile_worker_claim(config, "Host fenced; central confirmation recorded", client=client)
    assert result == {"status": "reconciled", "request_id": claim["request_id"]}
    journal = open_journal(worker)
    assert journal.pending_claim() is None
    row = journal._db.execute("SELECT payload,state FROM claim_attempts WHERE request_id=?", (claim["request_id"],)).fetchone()
    assert row["state"] == "reconciled" and "central confirmation" in row["payload"]
    journal.close()


def test_database_diagnostics_reports_stuck_work_without_modifying_it(world):
    from archon_horizon.pipeline.operations.diagnostics import database_checks
    world.run()
    from uuid import UUID
    execution = {"id": UUID(world.claim()["execution_id"])}
    world.conn.execute(tables["execution"].update().where(tables["execution"].c.id == execution["id"]).values(
        lease_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
    create(world.conn, "outbox_operation", project_id=world.project["id"], actor_principal_id=world.actor.id,
           kind="zulip_post", idempotency_key="diagnostic", payload={}, schema_version=1, status="uncertain")
    checks = {row["name"]: row for row in database_checks(world.conn)}
    assert checks["worker_leases"]["expired"] == 1
    assert checks["worker_leases"]["status"] == "attention"
    assert checks["outbox"]["counts"]["uncertain"] == 1
    assert checks["outbox"]["oldest_pending_age_seconds"] >= 0
    assert world.conn.execute(select(tables["execution"].c.status).where(tables["execution"].c.id == execution["id"])).scalar_one() == "running"


def test_worker_config_rejects_remote_http_and_overlapping_credentials(tmp_path):
    harness = {"id": str(uuid4()), "adapter": "codex_exec", "executable": "/usr/bin/codex",
               "provider_home": str(tmp_path / "provider"), "scratch_root": str(tmp_path / "scratch"),
               "sandbox": {"mode": "unrestricted"}}
    values = {"host_id": str(uuid4()), "api_url": "https://horizon.invalid", "token_file": str(tmp_path / "token"),
              "journal_root": str(tmp_path / "journal"), "workspace_roots": [str(tmp_path / "workspace")], "harnesses": [harness]}
    WorkerConfig.model_validate(values)
    for changed in ({"api_url": "http://horizon.invalid"}, {"workspace_roots": [str(tmp_path)]}, {"harnesses": [harness, harness]}):
        with pytest.raises(ValidationError):
            WorkerConfig.model_validate({**values, **changed})
    token = tmp_path / "token"
    token.write_text("private-token\n")
    token.chmod(0o600)
    assert private_text(token) == "private-token"
    link = tmp_path / "link"
    link.symlink_to(token)
    with pytest.raises(OSError):
        private_text(link)
    token.chmod(0o644)
    with pytest.raises(ValueError):
        private_text(token)


def test_worker_load_propagates_explicit_tool_environment(tmp_path):
    from archon_horizon.pipeline.worker_config import load_worker

    token = tmp_path / "host-token"
    token.write_text("private-host-token")
    token.chmod(0o600)
    harness_id = str(uuid4())
    environment = {"PATH": str(tmp_path / "tools") + ":/usr/bin:/bin",
                   "ELAN_HOME": str(tmp_path / "toolchains"), "LANG": "C.UTF-8"}
    config = tmp_path / "worker.json"
    config.write_text(json.dumps({
        "host_id": str(uuid4()), "api_url": "https://horizon.invalid", "token_file": str(token),
        "journal_root": str(tmp_path / "journal"), "workspace_roots": [str(tmp_path / "workspace")],
        "journal_min_free_bytes": 0,
        "harnesses": [{"id": harness_id, "adapter": "codex_exec", "executable": "/unused/codex",
                       "provider_home": str(tmp_path / "provider"), "scratch_root": str(tmp_path / "scratch"),
                       "sandbox": {"mode": "unrestricted"}, "environment": environment}],
    }))
    daemon, slots = load_worker(config)
    try:
        assert slots == 2
        assert daemon.harnesses[harness_id].environment == environment
    finally:
        daemon.journal.close()
        daemon.transport.client.close()


@pytest.mark.parametrize("environment", [
    {"HOME": "/other"}, {"CODEX_HOME": "/other"}, {"CLAUDE_CONFIG_DIR": "/other"},
    {"TMPDIR": "/other"}, {"HORIZON_HOST_TOKEN": "secret"},
    {"HORIZON_EXECUTION_TOKEN": "secret"}, {"HORIZON_API_URL": "https://other.invalid"},
    {"OPENAI_API_KEY": "secret"}, {"AWS_SECRET_ACCESS_KEY": "secret"},
    {"LD_PRELOAD": "/inject.so"}, {"BASH_ENV": "/inject"},
    {"PATH": "bin:/usr/bin"}, {"PATH": ":/usr/bin"}, {"PATH": "/usr/bin:"},
    {"PATH": "/tools/../bin:/usr/bin"}, {"PATH": "$PATH:/usr/bin"},
    {"ELAN_HOME": "~/.elan"}, {"LEAN_PATH": "/lib::/other"}, {"LANG": "C\nBAD=value"},
])
def test_worker_environment_rejects_credential_overrides_and_unsafe_paths(tmp_path, environment):
    from archon_horizon.pipeline.worker_config import LocalHarness
    from archon_horizon.pipeline.worker.daemon import HarnessConfig
    from archon_horizon.pipeline.worker.provider import HeadlessAdapter

    values = {"id": str(uuid4()), "adapter": "codex_exec", "executable": "/usr/bin/codex",
              "provider_home": str(tmp_path / "provider"), "scratch_root": str(tmp_path / "scratch"),
              "sandbox": {"mode": "unrestricted"}, "environment": environment}
    with pytest.raises(ValidationError):
        LocalHarness.model_validate(values)
    with pytest.raises(ValueError):
        HarnessConfig(HeadlessAdapter("codex_exec", "/usr/bin/codex"), tmp_path / "provider",
                      tmp_path / "scratch", unrestricted=True, environment=environment)


def test_agent_uncertain_intent_survives_restart_and_reuses_identity(tmp_path, monkeypatch):
    keys = []
    available = False
    def handler(request):
        keys.append(request.headers["Idempotency-Key"])
        if not available:
            raise httpx.ReadError("acknowledgement lost", request=request)
        return httpx.Response(200, json={"id": keys[0]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        client = AgentClient("https://horizon.invalid", "unpersisted-secret", "execution", tmp_path, client=transport)
        with pytest.raises(httpx.ReadError):
            client.request("POST", "/api/v3/commands", {"action": "test"})
        client.close()
        available = True
        restarted = AgentClient("https://horizon.invalid", "fresh-secret", "execution", tmp_path, client=transport)
        with pytest.raises(RetryDeferred):
            restarted.request("POST", "/api/v3/commands", {"action": "test"})
        retry_at = restarted.pending()[0]["retry_at"]
        monkeypatch.setattr("archon_horizon.pipeline.client.time.time", lambda: retry_at + 1)
        assert restarted.request("POST", "/api/v3/commands", {"action": "test"})["id"] == keys[0]
        assert len(set(keys)) == 1
        with restarted.connect() as conn:
            assert conn.execute("SELECT status FROM intent").fetchone()[0] == "completed"
        for path in tmp_path.iterdir():
            assert b"unpersisted-secret" not in path.read_bytes()
            assert b"fresh-secret" not in path.read_bytes()


def test_agent_resume_replays_with_current_authority_and_preserves_provenance(tmp_path):
    requests = []
    available = False
    def handler(request):
        requests.append(request)
        return httpx.Response(200 if available else 503, json={"status": "completed" if available else "unavailable"})
    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        old = AgentClient("https://horizon.invalid", "old-token", "old-execution", tmp_path,
                          client=transport, assignment_id="same-assignment")
        with pytest.raises(RuntimeError):
            old.request("POST", "/api/v3/commands", {"action": "work"})
        available = True
        resumed = AgentClient("https://horizon.invalid", "new-token", "new-execution", tmp_path,
                              client=transport, assignment_id="same-assignment")
        key = resumed.pending()[0]["id"]
        assert resumed.replay_pending() == {"completed": [key], "blocked": [], "pending": 0}
        assert requests[0].headers["idempotency-key"] == requests[1].headers["idempotency-key"]
        assert requests[1].headers["authorization"] == "Bearer new-token"
        with resumed.connect() as conn:
            assert conn.execute("SELECT execution_id FROM intent").fetchone()[0] == "old-execution"
        with pytest.raises(ValueError, match="another assignment"):
            AgentClient("https://horizon.invalid", "token", "execution", tmp_path, client=transport, assignment_id="other-assignment")


def test_rejected_intent_requires_authenticated_central_repair_before_local_resolution(tmp_path):
    assignment_id, obligation_id = str(uuid4()), str(uuid4())
    repair_available = False
    requests = []
    def handler(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"assignment_id": assignment_id})
        if request.url.path == "/api/v3/commands":
            return httpx.Response(409, json={"error": {"message": "revision conflict"}})
        if request.url.path == "/api/v3/records/obligation":
            return httpx.Response(200, json={"id": obligation_id, "revision": 1})
        return httpx.Response(200 if repair_available else 503, json={"id": obligation_id, "revision": 2})
    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        client = AgentClient("https://horizon.invalid", "current-token", "execution", tmp_path,
                             assignment_id=assignment_id, client=transport)
        with pytest.raises(RuntimeError):
            client.request("POST", "/api/v3/commands", {"action": "obsolete"})
        key = client.pending()[0]["id"]
        assert client.replay_pending()["blocked"]
        assert client.replay_pending()["pending"] == 1
        with pytest.raises(RuntimeError):
            client.resolve_intent(key, "The conflicting change was inspected and a corrected command completed.")
        assert next(item for item in client.pending() if item["id"] == key)["status"] == "rejected"
        repair_available = True
        result = client.resolve_intent(key, "The conflicting change was inspected and a corrected command completed.")
        assert result["status"] == "resolved" and result["obligation_id"] == obligation_id
        assert client.pending() == []
        create_calls = [request for request in requests if request.url.path == "/api/v3/records/obligation"]
        assert len({request.headers["idempotency-key"] for request in create_calls}) == 1
        assert all(request.headers["authorization"] == "Bearer current-token" for request in requests)


def test_agent_preserves_but_refuses_intents_outside_replay_window(tmp_path):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(503, json={"error": {"message": "offline"}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        client = AgentClient("https://horizon.invalid", "token", "execution", tmp_path, client=transport, max_offline_seconds=60)
        with pytest.raises(RuntimeError):
            client.request("POST", "/api/v3/commands", {"action": "test"})
        with client.connect() as conn:
            conn.execute("UPDATE intent SET created_at=0")
        with pytest.raises(RuntimeError, match="reconcile"):
            client.request("POST", "/api/v3/commands", {"action": "test"})
        assert len(calls) == 1
        with client.connect() as conn:
            assert conn.execute("SELECT count(*) FROM intent WHERE status='pending'").fetchone()[0] == 1


def test_cleanup_preserves_protected_data_and_rechecks_pin(tmp_path):
    config = PipelineConfig(database_url="postgresql+psycopg://a:b@127.0.0.1/test", state_root=tmp_path)
    manager = storage.StorageManager(config)
    terminal = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    marker = {"schema_version": 1, "terminal_at": terminal, "failure_resolved": True, "pinned": False}
    for name in ("eligible", "repinned", "unresolved", "young", "symlinked"):
        path = tmp_path / "diagnostics" / name
        path.mkdir(parents=True)
        current = {**marker}
        if name == "unresolved":
            current["failure_resolved"] = False
        if name == "young":
            current["terminal_at"] = datetime.now(timezone.utc).isoformat()
        (path / "retention.json").write_text(json.dumps(current))
        (path / "output").write_text("verbose diagnostic")
    protected = tmp_path / "artifacts" / "proof"
    protected.parent.mkdir()
    protected.write_text("durable evidence")
    (tmp_path / "diagnostics" / "symlinked" / "outside").symlink_to(protected)
    preview = manager.cleanup_preview()
    assert {Path(item["path"]).name for item in preview["candidates"]} == {"eligible", "repinned"}
    (tmp_path / "diagnostics" / "repinned" / "retention.json").write_text(json.dumps({**marker, "pinned": True}))
    result = manager.cleanup(preview)
    assert len(result["removed"]) == 1
    assert protected.read_text() == "durable evidence"
    assert (tmp_path / "diagnostics" / "repinned").exists()
    assert manager.cleanup(preview)["removed"] == []


def test_external_watchdog_uses_liveness_threshold_and_restart_cooldown(tmp_path):
    from archon_horizon.pipeline.operations.watchdog import check
    config = PipelineConfig(database_url="postgresql+psycopg://a:b@127.0.0.1/test", state_root=tmp_path)
    healthy = False
    restarts = []
    def response(request):
        assert request.url.path == "/health/live"
        return httpx.Response(200 if healthy else 503, json={"status": "alive"})
    def restart(command, **kwargs):
        restarts.append(command)
        return subprocess.CompletedProcess(command, 0)
    with httpx.Client(transport=httpx.MockTransport(response)) as probe:
        def observe(now):
            return check(config, service="archon-horizon-pipeline-api.service", client=probe, runner=restart, now=now)
        assert not observe(0)["restart_requested"]
        assert not observe(30)["restart_requested"]
        assert observe(60)["restart_requested"]
        assert not observe(90)["restart_requested"]
        assert observe(360)["restart_requested"]
        healthy = True
        assert observe(390)["consecutive_failures"] == 0
        assert restarts == [["systemctl", "--user", "--no-block", "try-restart", "archon-horizon-pipeline-api.service"]] * 2
        with pytest.raises(ValueError):
            check(config, service="unrelated.service", client=probe, runner=restart)


def test_worker_example_is_valid_after_operator_resolves_required_pins():
    example = Path(__file__).parents[1] / "deploy/pipeline/worker.example.json"
    data = json.loads(example.read_bytes())
    data["harnesses"][0]["sandbox"]["image_digest"] = "localhost/horizon@sha256:" + "a" * 64
    WorkerConfig.model_validate(data)


def test_owned_service_logs_plateau_with_multibyte_and_oversized_messages(tmp_path):
    config = PipelineConfig(database_url="postgresql+psycopg://a:b@127.0.0.1/test", state_root=tmp_path,
                            storage={"service_log_budget_bytes": 16384})
    logger = logging.getLogger("archon_horizon.pipeline.operations_test")
    with storage.service_logging(config):
        for number in range(300):
            logger.info("%s %s", number, "\u03bb" * 20000)
    files = list((tmp_path / "logs").glob("service.log*"))
    assert len(files) <= 4
    assert sum(path.stat().st_size for path in files) <= config.storage.service_log_budget_bytes
    assert any("diagnostic record truncated" in path.read_text() for path in files)


def test_young_resolved_diagnostics_obey_budget_without_deleting_pins(tmp_path):
    config = PipelineConfig(database_url="postgresql+psycopg://a:b@127.0.0.1/test", state_root=tmp_path,
                            storage={"diagnostic_budget_bytes": 1500})
    for number in range(4):
        directory = tmp_path / "diagnostics" / str(number)
        directory.mkdir(parents=True)
        (directory / "output").write_text("x" * 1000)
        (directory / "retention.json").write_text(json.dumps({"schema_version": 2,
            "terminal_at": (datetime.now(timezone.utc) - timedelta(hours=4-number)).isoformat(),
            "failure_resolved": True, "pinned": number == 3, "outcome": "succeeded"}))
    manager = storage.StorageManager(config)
    preview = manager.cleanup_preview()
    assert len(preview["candidates"]) == 2
    assert all(item["reason"] == "diagnostic_budget" for item in preview["candidates"])
    manager.cleanup(preview)
    assert (tmp_path / "diagnostics" / "3" / "output").exists()
    assert manager.cleanup_preview()["retained_diagnostic_bytes"] <= 1500


def test_backup_rotation_keeps_newest_verified_and_rechecks_corruption(tmp_path):
    config = PipelineConfig(database_url="postgresql+psycopg://a:b@127.0.0.1/test", state_root=tmp_path,
                            storage={"backup_keep_count": 1, "backup_budget_bytes": 1})
    for number in range(3):
        directory = tmp_path / "backups" / str(number)
        directory.mkdir(parents=True)
        (directory / "database.dump").write_bytes(b"test-only-checksum-fixture")
        (directory / "blobs").mkdir()
        (directory / "manifest.json").write_text(json.dumps({"schema_version": 1,
            "created_at": f"2026-01-0{number+1}T00:00:00+00:00", "artifacts": [],
            "database_sha256": hashlib.sha256(b"test-only-checksum-fixture").hexdigest()}))
    manager = storage.StorageManager(config)
    preview = manager.cleanup_preview()
    assert {Path(item["path"]).name for item in preview["candidates"]} == {"0", "1"}
    (tmp_path / "backups" / "0" / "database.dump").write_bytes(b"corrupt")
    result = manager.cleanup(preview)
    assert len(result["removed"]) == 1
    assert (tmp_path / "backups" / "0").exists()
    assert (tmp_path / "backups" / "2").exists()
    assert manager.backup_preview()["over_budget"]
    assert manager.backup_preview()["candidates"] == []


def test_database_retention_preserves_unsettled_live_and_referenced_history(world):
    from archon_horizon.pipeline.persistence.records import get
    old = datetime.now(timezone.utc) - timedelta(days=40)
    run = world.run()
    assignment = world.assignment(run)
    claim = world.claim()
    agent = world.conn.execute(select(tables["principal"]).where(tables["principal"].c.execution_id == claim["execution_id"])).mappings().one()
    def receipt(status="completed", principal_id=None, expires_at=old):
        return create(world.conn, "api_request", principal_id=principal_id or world.actor.id,
            operation="test", idempotency_key=str(uuid4()), request_sha256="a" * 64, status=status,
            response={} if status == "completed" else None, expires_at=expires_at, created_at=old)
    expired = receipt()
    pending = receipt("pending")
    live = receipt(principal_id=agent["id"])
    recent = receipt(expires_at=datetime.now(timezone.utc) + timedelta(days=1))
    def event(kind="record_changed"):
        return create(world.conn, "event", project_id=world.project["id"], kind=kind, schema_version=1,
            source="control_plane", source_event_id=str(uuid4()), occurred_at=old, payload={}, created_at=old)
    free, referenced, repinned, semantic = event(), event(), event(), event("status_changed")
    create(world.conn, "notification", assignment_id=assignment["id"], event_id=referenced["id"], urgency="routine")
    preview = storage.database_retention_preview(world.conn, world.service.config)
    assert {item["id"] for item in preview["api_requests"]} == {str(expired["id"])}
    assert set(preview["events"]) == {str(free["id"]), str(repinned["id"])}
    create(world.conn, "notification", assignment_id=assignment["id"], event_id=repinned["id"], urgency="routine")
    result = storage.apply_database_retention(world.conn, world.service.config, preview)
    assert result == {"api_requests_removed": 1, "events_removed": 1}
    for protected in (pending, live, recent):
        assert get(world.conn, "api_request", protected["id"])
    for protected in (referenced, repinned, semantic):
        assert get(world.conn, "event", protected["id"])


def resolved_project_recipe():
    from archon_horizon.pipeline.projects.bootstrap import ProjectRecipe
    raw = json.loads((Path(__file__).parents[1] / "deploy/pipeline/project.example.json").read_bytes())
    raw["id"] = str(uuid4())
    for record in raw["records"]:
        values = record["values"]
        if "sandbox" in values:
            values["sandbox"]["image_digest"] = "localhost/horizon@sha256:" + "a" * 64
        for field in ("source_commit_oid", "base_commit_oid"):
            if field in values:
                values[field] = "b" * 40
    return ProjectRecipe.model_validate(raw)


def test_project_recipe_preview_expands_optional_phase_reviewers_without_io():
    from archon_horizon.pipeline.projects import bootstrap
    recipe = resolved_project_recipe()
    result = bootstrap.preview(recipe)
    assert result["external_effects"] == [] and not result["starts_runs"]
    policies = [row for row in result["records"] if row["kind"] == "review_policy"]
    assert len(policies) == 3
    assert {row["values"]["phases"][0] for row in policies} == {"preprocessing", "formalization", "postprocessing"}
    assert all(row["values"]["repository_ids"] != [{"$ref": "workspace"}] for row in policies)
    with pytest.raises(ValueError, match="earlier record"):
        bootstrap.resolve({"$ref": "later"}, {})


def test_project_bootstrap_is_atomic_idempotent_and_launch_is_separate(world):
    from archon_horizon.pipeline.projects import bootstrap
    from archon_horizon.pipeline.errors import DomainError
    recipe = resolved_project_recipe()
    first = bootstrap.apply(world.conn, world.actor, world.service, recipe)
    assert bootstrap.apply(world.conn, world.actor, world.service, recipe) == first
    project_id = first["records"]["project"]["id"]
    assert world.conn.execute(select(tables["mission"].c.id).where(tables["mission"].c.project_id == project_id)).first()
    assert world.conn.execute(select(tables["workspace"].c.id).where(tables["workspace"].c.project_id == project_id)).all()
    assert not world.conn.execute(select(tables["run"].c.id).join(tables["mission"]).where(tables["mission"].c.project_id == project_id)).first()
    assert world.conn.execute(select(tables["review_policy"].c.id).where(tables["review_policy"].c.project_id == project_id)).all()
    changed = recipe.model_copy(deep=True)
    changed.records[0].values["title"] = "Different recipe"
    with pytest.raises(DomainError, match="different contents"):
        bootstrap.apply(world.conn, world.actor, world.service, changed)
    launch = bootstrap.RunRecipe(id=uuid4(), run={"orchestration": "legacy", "mission_id": world.mission["id"],
        "phase": {"kind": "preprocessing", "roadmap_document_id": world.document["id"]}, "host_ids": [world.host["id"]]})
    run = bootstrap.launch(world.conn, world.actor, world.scheduler, launch)
    assert bootstrap.launch(world.conn, world.actor, world.scheduler, launch)["id"] == run["id"]


def test_invalid_bootstrap_rolls_back_all_created_records(world):
    from archon_horizon.pipeline.projects import bootstrap
    from archon_horizon.pipeline.errors import DomainError
    recipe = resolved_project_recipe()
    recipe.records[0].values["slug"] = "invalid_bootstrap"
    recipe.review_presets[0].repository_id = {"$ref": "workspace"}
    with pytest.raises(DomainError, match="knowledge repository"):
        with world.conn.begin_nested():
            bootstrap.apply(world.conn, world.actor, world.service, recipe)
    assert not world.conn.execute(select(tables["project"].c.id).where(tables["project"].c.slug == "invalid_bootstrap")).first()
    assert not world.conn.execute(select(tables["api_request"].c.id).where(tables["api_request"].c.idempotency_key == str(recipe.id))).first()


def test_local_workspace_verification_checks_git_without_mutating_files(world, tmp_path):
    from archon_horizon.pipeline.auth import issue_credential
    from archon_horizon.pipeline.persistence.records import get
    from archon_horizon.pipeline.execution.workspace_setup import verify
    root = Path(world.host["workspace_root"])
    checkout = root / "initial"
    checkout.mkdir(parents=True)
    env = {"PATH": os.defpath, "HOME": str(tmp_path), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid", "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid"}
    def git(*args):
        return subprocess.run(["git", *args], cwd=checkout, env=env, capture_output=True, check=True).stdout.decode().strip()
    git("init", "--initial-branch=worker")
    (checkout / "Proof.lean").write_text("-- Initial operator checkout\n")
    (checkout / ".gitattributes").write_text("Proof.lean filter=unsafe\n")
    git("add", ".")
    git("commit", "-m", "Initial checkout")
    head = git("rev-parse", "HEAD")
    marker = tmp_path / "filter-must-not-execute"
    git("config", "filter.unsafe.clean", f"touch {marker}")
    git("config", "filter.unsafe.required", "true")
    os.utime(checkout / "Proof.lean", None)
    workspace = create(world.conn, "workspace", project_id=world.project["id"], host_id=world.host["id"],
        repository_id=world.workspace_repo["id"], path=str(checkout), branch_name="worker", base_commit_oid=head)
    _, token = issue_credential(world.conn, world.host_actor.id, "host_key", "Provisioning test")
    token_file = tmp_path / "host-token"
    token_file.write_text(token)
    token_file.chmod(0o600)
    worker = WorkerConfig.model_validate({"host_id": world.host["id"], "api_url": "https://horizon.invalid",
        "token_file": token_file, "journal_root": tmp_path / "journal", "workspace_roots": [root],
        "harnesses": [{"id": world.harness["id"], "adapter": "codex_exec", "executable": "/bin/codex",
            "provider_home": tmp_path / "provider", "scratch_root": tmp_path / "scratch", "sandbox": {"mode": "unrestricted"}}]})
    class DatabaseView:
        def transaction(self):
            return nullcontext(world.conn)
    preview = verify(DatabaseView(), world.actor, worker)
    assert preview["workspaces"][0]["ready"], preview
    assert not marker.exists()
    assert get(world.conn, "workspace", workspace["id"])["status"] == "preparing"
    dirty = checkout / "unpublished.lean"
    dirty.write_text("-- Preserve this work\n")
    refused = verify(DatabaseView(), world.actor, worker)
    assert not refused["workspaces"][0]["ready"]
    assert dirty.read_text() == "-- Preserve this work\n"
    dirty.unlink()
    applied = verify(DatabaseView(), world.actor, worker, apply=True)
    assert applied["applied"]
    assert get(world.conn, "workspace", workspace["id"])["status"] == "ready"
    assert not marker.exists()


def test_postgres_backup_restores_database_and_matching_blobs(service_database, tmp_path, monkeypatch):
    container = os.environ.get("HORIZON_PIPELINE_BACKUP_TEST_CONTAINER")
    if not container:
        pytest.skip("set HORIZON_PIPELINE_BACKUP_TEST_CONTAINER to the isolated development PostgreSQL container")
    run = subprocess.run
    inspection = run(["docker", "inspect", "--format", '{{ index .Config.Labels "archon-horizon.scope" }}', container],
                     check=True, capture_output=True, timeout=20)
    assert inspection.stdout.strip() == b"pipeline-development"
    source_url = make_url(os.environ["HORIZON_PIPELINE_TEST_URL"])
    config = PipelineConfig(database_url=source_url.render_as_string(hide_password=False), state_root=tmp_path / "state")
    content = ArtifactStore(config.artifact_root).put(b"verified theorem evidence", "text/plain")
    with service_database.transaction() as conn:
        project = create(conn, "project", number=999, slug="backup_" + uuid4().hex, title="Backup proof")
        artifact = create(conn, "artifact", project_id=project["id"], kind="blob", content=content)
        create(conn, "artifact_location", artifact_id=artifact["id"], locator=content["sha256"])

    def dump_in_test_container(command, **kwargs):
        assert command[0] == "test-pg-dump"
        target = Path(next(arg.removeprefix("--file=") for arg in command if arg.startswith("--file=")))
        args = [arg for arg in command[1:] if not arg.startswith("--file=")]
        with target.open("wb") as output:
            return run(["docker", "exec", container, "pg_dump", "-U", source_url.username, "-d", source_url.database, *args],
                       stdout=output, stderr=subprocess.PIPE, timeout=kwargs["timeout"])
    monkeypatch.setattr(storage.subprocess, "run", dump_in_test_container)
    destination = tmp_path / "backup"
    storage.backup(service_database, config, destination, pg_dump="test-pg-dump")
    manifest = storage.verify_backup(destination)
    assert manifest["artifacts"][0]["sha256"] == content["sha256"]
    assert source_url.password not in (destination / "manifest.json").read_text()
    target_name = "pipeline_restore_" + uuid4().hex
    import psycopg
    from psycopg import sql
    with psycopg.connect(source_url.set(drivername="postgresql").render_as_string(hide_password=False), autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target_name)))
        try:
            with (destination / "database.dump").open("rb") as dump:
                run(["docker", "exec", "-i", container, "pg_restore", "-U", source_url.username, "-d", target_name,
                     "--exit-on-error", "--single-transaction", "--no-owner", "--no-acl"], stdin=dump, check=True,
                    capture_output=True, timeout=60)
            with psycopg.connect(source_url.set(drivername="postgresql", database=target_name).render_as_string(hide_password=False)) as restored:
                row = restored.execute(sql.SQL("SELECT content FROM {}.artifact WHERE id = %s").format(
                    sql.Identifier(service_database.schema)), (artifact["id"],)).fetchone()
                assert row[0]["sha256"] == content["sha256"]
                restored_root = tmp_path / "restored-artifacts"
                blob = (destination / "blobs" / content["sha256"]).read_bytes()
                recovered = ArtifactStore(restored_root).put(blob, content["media_type"])
                assert recovered == content
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(target_name)))
    (destination / "blobs" / content["sha256"]).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="checksum"):
        storage.verify_backup(destination)
