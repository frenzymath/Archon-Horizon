from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

from archon_horizon.pipeline.worker.contracts import ExecutionGrant, FencedExecution, JournalFull, Operation
from archon_horizon.pipeline.worker.daemon import HarnessConfig, WorkerDaemon
from archon_horizon.pipeline.worker.git_recovery import GitRecovery
from archon_horizon.pipeline.worker.journal import DurableJournal
from archon_horizon.pipeline.worker.provider import HeadlessAdapter, ProcessSupervisor, process_identity
from archon_horizon.pipeline.worker.journal import boot_identity
from archon_horizon.pipeline.worker.sandbox import SandboxMount, SandboxPolicy, podman_command
from archon_horizon.pipeline.worker.transport import WorkerTransport


@pytest.fixture
def journal(tmp_path):
    value = DurableJournal(tmp_path / "state", minimum_free_bytes=0)
    yield value
    value.close()


def operation(**kwargs):
    return Operation.create("execution-1", 1, "activity", {"summary": "durable work"}, **kwargs)


def test_journal_restart_lost_ack_and_claim_fencing(tmp_path):
    root = tmp_path / "state"
    journal = DurableJournal(root, minimum_free_bytes=0)
    item = operation()
    assert journal.enqueue(item)
    assert not journal.enqueue(item)
    first = journal.claim(claim_seconds=0.001)
    assert first is not None
    journal.close()
    journal = DurableJournal(root, minimum_free_bytes=0)
    second = journal.claim(now=time.time() + 1)
    assert second and second.operation == item
    with pytest.raises(FencedExecution):
        journal.settle(first, "acknowledged")
    journal.settle(second, "acknowledged")
    assert journal.claim() is None
    with pytest.raises(ValueError, match="different content"):
        journal.enqueue(Operation.create("execution-1", 1, "activity", {"summary": "different"},
                                         operation_id=item.operation_id, occurred_at=item.occurred_at))
    assert journal.compact(acknowledged_before=time.time() + 1) == 1
    journal.close()


def test_journal_bounded_protected_data_and_expired_replay(tmp_path):
    journal = DurableJournal(tmp_path / "state", max_bytes=256 * 1024, minimum_free_bytes=0,
                             max_offline_seconds=10)
    count = 0
    with pytest.raises(JournalFull):
        for index in range(100):
            journal.enqueue(Operation.create("execution-1", 1, "activity", {"data": "x" * 8192, "n": index}))
            count += 1
    assert count > 0
    assert len(journal.records()) == count
    assert journal.compact(acknowledged_before=time.time() + 100) == 0
    assert journal.claim(now=time.time() + 20) is None
    assert len(journal.records("recovery")) == count
    journal.close()


def test_monotonic_lease_cannot_revive_expired_or_rebooted_owner(journal):
    journal.grant_lease("execution-1", 1, 10, boot_id="boot", monotonic_now=100)
    journal.assert_lease("execution-1", 1, boot_id="boot", monotonic_now=109)
    with pytest.raises(FencedExecution):
        journal.assert_lease("execution-1", 1, boot_id="boot", monotonic_now=110)
    with pytest.raises(FencedExecution):
        journal.grant_lease("execution-1", 1, 10, boot_id="boot", monotonic_now=110)
    with pytest.raises(FencedExecution):
        journal.assert_lease("execution-1", 1, boot_id="another-boot", monotonic_now=1)
    journal.grant_lease("execution-1", 2, 10, boot_id="boot", monotonic_now=110)
    with pytest.raises(FencedExecution):
        journal.assert_lease("execution-1", 1, boot_id="boot", monotonic_now=111)


def test_transport_retry_after_and_auth_failure_preserve_payload(journal):
    sent = []
    statuses = [429, 200, 401]

    def handler(request):
        sent.append(request)
        return httpx.Response(statuses.pop(0), headers={"Retry-After": "120"})

    transport = WorkerTransport("http://testserver", "host-secret", client=httpx.Client(transport=httpx.MockTransport(handler)))
    item = operation()
    journal.enqueue(item)
    now = time.time()
    assert transport.replay_one(journal, now=now) == "pending"
    assert journal.records()[0]["retry_at"] >= now + 120
    assert transport.replay_one(journal, now=now + 121) == "acknowledged"
    assert sent[0].headers["Idempotency-Key"] == sent[1].headers["Idempotency-Key"]
    assert sent[0].content == sent[1].content
    journal.enqueue(operation())
    assert transport.replay_one(journal) == "blocked"
    assert len(journal.records("blocked")) == 1
    assert b"host-secret" not in journal.path.read_bytes()


def test_transport_lost_response_reuses_identity_and_exhaustion_retains_recovery(journal):
    calls = []

    def handler(request):
        calls.append(request.headers["Idempotency-Key"])
        raise httpx.ReadTimeout("lost response", request=request)

    transport = WorkerTransport("http://testserver", "secret", max_attempts=2,
                                client=httpx.Client(transport=httpx.MockTransport(handler)))
    journal.enqueue(operation())
    now = time.time()
    assert transport.replay_one(journal, now=now) == "pending"
    assert transport.replay_one(journal, now=now + 5) == "recovery"
    assert calls[0] == calls[1]
    assert len(journal.records("recovery")) == 1


def test_lost_claim_response_replays_durable_key_before_new_admission(tmp_path):
    state = tmp_path / "claim-journal"
    first = DurableJournal(state)
    attempt = first.claim_attempt("host", ["harness"])
    first.close()
    journal = DurableJournal(state)
    grant = ExecutionGrant("execution", "assignment", 1, 60, "harness", "workspace", str(tmp_path), "repo", "Goal",
                            execution_token="never-persist-this-token")
    claims = []

    def handler(request):
        if request.url.path.endswith("/claim"):
            claims.append(request.headers["Idempotency-Key"])
            if len(claims) == 1:
                raise httpx.ReadTimeout("Server committed but response was lost", request=request)
            return httpx.Response(200, json={"execution": grant.__dict__ if len(claims) == 2 else None})
        return httpx.Response(200, json={"acknowledged": True})

    transport = WorkerTransport("http://testserver", "host-secret", client=httpx.Client(transport=httpx.MockTransport(handler)))
    config = HarnessConfig(HeadlessAdapter("codex_exec", "/missing-provider"), tmp_path / "home", tmp_path / "scratch", unrestricted=True)
    daemon = WorkerDaemon(host_id="host", journal=journal, transport=transport, harnesses={"harness": config}, workspace_roots=(tmp_path,))
    try:
        with pytest.raises(httpx.ReadTimeout):
            daemon.run_once()
        assert journal.pending_claim()["request_id"] == attempt["request_id"]
        assert daemon.run_once() == "failed"
        assert journal.pending_claim() is None
        assert daemon.run_once() is None
        assert claims[0] == claims[1] == attempt["request_id"] and claims[2] != claims[1]
        assert b"never-persist-this-token" not in journal.path.read_bytes()
        assert journal.executions()[0]["status"] == "failed"
    finally:
        journal.close()


def test_ancient_claim_requires_explicit_reconciliation(journal, monkeypatch):
    pending = journal.claim_attempt("host", ["harness"])
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now + journal.max_offline_seconds + 1)
    with pytest.raises(RuntimeError, match="reconcile host"):
        journal.claim_attempt("host", ["harness"])
    assert journal.pending_claim()["request_id"] == pending["request_id"]
    journal.abandon_claim(pending["request_id"], "Operator verified every host execution physically stopped")
    assert journal.claim_attempt("host", ["harness"])["request_id"] != pending["request_id"]


def test_expired_claim_replay_records_stop_before_workspace_or_provider_access(journal, tmp_path, monkeypatch):
    from dataclasses import replace

    grant = ExecutionGrant("expired", "assignment", 1, 0, "harness", "workspace", str(tmp_path / "missing-workspace"), "repo", "Goal")
    with pytest.raises(ValueError, match="lease"):
        replace(grant, lease_seconds=-1)
    observed = []

    def handler(request):
        if request.url.path.endswith("/claim"):
            return httpx.Response(200, json={"execution": grant.__dict__})
        observed.append(json.loads(request.content))
        return httpx.Response(200, json={"acknowledged": True})

    transport = WorkerTransport("http://testserver", "host-secret", client=httpx.Client(transport=httpx.MockTransport(handler)))
    config = HarnessConfig(HeadlessAdapter("codex_exec", "/must-not-launch"), tmp_path / "home", tmp_path / "scratch", unrestricted=True)
    daemon = WorkerDaemon(host_id="host", journal=journal, transport=transport, harnesses={"harness": config}, workspace_roots=(tmp_path / "missing-workspace",))
    def forbidden(*args, **kwargs):
        raise AssertionError("Expired claim must not access the workspace or executable")
    monkeypatch.setattr(daemon, "_workspace", forbidden)
    monkeypatch.setattr(daemon, "_validate_executable", forbidden)
    assert daemon.run_once() == "lost"
    assert journal.pending_claim() is None
    assert journal.executions()[0]["status"] == "lost"
    assert len(observed) == 1 and observed[0]["kind"] == "execution_finished"
    assert observed[0]["payload"] == {"status": "lost", "reason": "claim_expired_before_launch"}
    assert not config.provider_home.exists() and not config.scratch_root.exists()


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=True).stdout.strip()


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    git(root, "init")
    git(root, "config", "user.name", "Worker test")
    git(root, "config", "user.email", "test@example.invalid")
    (root / "Proof.lean").write_text("theorem one : True := True.intro\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "Initial proof")
    return root


def test_git_commit_before_journal_gap_reflog_and_remote_preservation(repository, journal, tmp_path):
    recovery = GitRecovery(repository, "repo-1", "execution-1", 1, journal)
    initial = git(repository, "rev-parse", "HEAD")
    (repository / "Proof.lean").write_text("theorem two : True := True.intro\n")
    git(repository, "commit", "-am", "Unreported commit")
    unpublished = git(repository, "rev-parse", "HEAD")
    # Simulate an agent moving the branch before the daemon receives its API report.
    git(repository, "reset", "--hard", initial)
    assert unpublished in recovery.reconcile(limit=1)
    assert recovery.reconcile() == []
    assert git(repository, "rev-parse", recovery.prefix + unpublished) == unpublished
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(bare))
    remote_ref = recovery.preserve_remote(unpublished, remote=str(bare))
    assert remote_ref.startswith("refs/heads/horizon/recovery/")
    assert git(bare, "rev-parse", remote_ref) == unpublished
    assert recovery.preserve_remote(unpublished, remote=str(bare)) == remote_ref


def test_preserved_commit_is_available_as_broker_pull_request_head(repository, journal, tmp_path):
    from archon_horizon.pipeline.integrations.connectors import ForgejoClient

    recovery = GitRecovery(repository, "repo-1", "execution-1", 1, journal)
    oid = git(repository, "rev-parse", "HEAD")
    remote = tmp_path / "forge-repo.git"
    git(tmp_path, "init", "--bare", str(remote))
    branch = recovery.preserve_remote(oid, remote=str(remote)).removeprefix("refs/heads/")

    def handler(request):
        if request.url.path.endswith("/user"):
            return httpx.Response(200, json={"id": 1})
        if request.method == "GET":
            return httpx.Response(200, json=[])
        data = json.loads(request.content)
        assert git(remote, "rev-parse", "refs/heads/" + data["head"]) == oid
        return httpx.Response(201, json={"number": 1, "title": data["title"], "state": "open",
                                        "head": {"sha": oid}, "user": {"id": 1}})

    broker = ForgejoClient("https://forge.invalid", "broker-secret", client=httpx.Client(transport=httpx.MockTransport(handler)))
    result = broker.create_item("owner/library", kind="pull_request", title="Proof", body="Proof evidence",
                                 operation_id="pr-1", head=branch, base="main")
    assert result["head"]["sha"] == oid


def test_git_dirty_checkpoint_keeps_worktree_branch_and_index(repository, journal):
    recovery = GitRecovery(repository, "repo-1", "execution-1", 1, journal)
    old_head = git(repository, "rev-parse", "HEAD")
    (repository / "Proof.lean").write_text("changed and uncommitted\n")
    (repository / "New.lean").write_text("new untracked proof\n")
    before_index = git(repository, "diff", "--cached")
    before_status = git(repository, "status", "--porcelain")
    snapshot = recovery.checkpoint_dirty()
    assert snapshot
    assert git(repository, "show", snapshot + ":New.lean") == "new untracked proof"
    assert git(repository, "rev-parse", "HEAD") == old_head
    assert git(repository, "diff", "--cached") == before_index
    assert git(repository, "status", "--porcelain") == before_status
    assert recovery.checkpoint_dirty() == snapshot


def test_git_conflicted_checkout_is_preserved_without_changing_index(repository, journal):
    git(repository, "checkout", "-b", "other")
    (repository / "Proof.lean").write_text("other proof\n")
    git(repository, "commit", "-am", "Other version")
    git(repository, "checkout", "-b", "current", "HEAD~1")
    (repository / "Proof.lean").write_text("current proof\n")
    git(repository, "commit", "-am", "Current version")
    merged = subprocess.run(["git", "merge", "other"], cwd=repository, capture_output=True)
    assert merged.returncode == 1
    before = (repository / "Proof.lean").read_text()
    index = (repository / ".git" / "index").read_bytes()
    recovery = GitRecovery(repository, "repo-1", "execution-1", 1, journal)
    snapshot = recovery.checkpoint_dirty()
    assert snapshot
    assert git(repository, "show", snapshot + ":Proof.lean") == before.strip()
    assert (repository / ".git" / "index").read_bytes() == index


@pytest.mark.parametrize("remote_code, expected", [("rate_limit_exceeded", "rate_limited"),
    ("authentication_error", "authentication_failed"), ("overloaded_error", "provider_overloaded")])
def test_structured_provider_failure_survives_durable_process_result(journal, repository, tmp_path, remote_code, expected):
    script = tmp_path / "provider-error.py"
    event = {"type": "turn.failed", "error": {"code": remote_code, "message": "Sensitive provider detail"}}
    script.write_text("import json\nprint(" + repr(json.dumps(event)) + ")\n")
    journal.grant_lease("execution-1", 1, 30)
    supervisor = ProcessSupervisor(journal)
    result = supervisor.run([sys.executable, str(script)], prompt="Work", request_id="failure-request",
                            execution_id="execution-1", epoch=1, workspace=repository, env={"PATH": os.defpath})
    assert result.status == "failed"
    assert result.failure["code"] == expected
    assert "Sensitive" not in result.failure["message"]
    assert json.loads(journal.request("failure-request")["result"])["failure"]["code"] == expected


@pytest.mark.parametrize("outcome, days", [("succeeded", 8), ("failed", 31)])
def test_diagnostic_retention_requires_terminal_recovery_and_all_delivery_ack(journal, repository, outcome, days):
    journal.grant_lease("execution-1", 1, 30)
    result = ProcessSupervisor(journal).run([sys.executable, "-c", "print('diagnostic evidence')"], prompt="Work",
        request_id="diagnostic-request", execution_id="execution-1", epoch=1, workspace=repository, env={"PATH": os.defpath})
    journal.fence("execution-1", 1, outcome)
    journal.checkpoint("execution-1", 1, {"recovery_pending": False})
    finished = Operation.create("execution-1", 1, "execution_finished", {"status": outcome})
    journal.enqueue(finished)
    later = time.time() + days * 86400
    assert journal.prune_diagnostics(now=later) == 0
    delivered = journal.claim()
    journal.settle(delivered, "acknowledged")
    assert journal.prune_diagnostics(now=time.time()) == 0
    assert journal.prune_diagnostics(now=later) > 0
    assert not Path(result.stdout_path).exists()
    assert journal.request("diagnostic-request")["state"] == "completed"
    assert journal.request("diagnostic-request")["diagnostics_pruned_at"] == later


def test_diagnostic_reservations_pause_before_overcommitting_budget(tmp_path):
    journal = DurableJournal(tmp_path / "state", diagnostic_max_bytes=4096, minimum_free_bytes=0)
    journal.reserve_diagnostics("request-1", "execution-1", 1, 3072)
    with pytest.raises(JournalFull):
        journal.reserve_diagnostics("request-2", "execution-2", 1, 2048)
    assert not journal.diagnostic_capacity(2048)
    journal.release_diagnostics("request-1")
    assert journal.diagnostic_capacity(4096)
    journal.close()


def test_diagnostic_usage_ignores_files_replaced_during_scan(tmp_path, monkeypatch):
    journal = DurableJournal(tmp_path / "state", minimum_free_bytes=0)
    request = journal.state_root / "requests" / "request-1"
    request.mkdir(parents=True)
    (request / "stdout.jsonl").write_bytes(b"output")
    (request / "child-lifecycle.tmp").write_bytes(b"cursor")
    original_stat = Path.stat
    calls = 0

    def replace_during_stat(path, *args, **kwargs):
        nonlocal calls
        if path.name == "child-lifecycle.tmp":
            calls += 1
            if calls > 1:
                raise FileNotFoundError(path)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", replace_during_stat)
    used, sizes = journal._diagnostic_usage()

    assert used == len(b"output")
    assert sizes == {"request-1": len(b"output")}
    journal.close()


def test_git_recovery_preserves_distinct_staged_content_and_ignores_host_execution_hooks(repository, journal, tmp_path):
    marker = tmp_path / "must-not-run"
    git(repository, "config", "filter.evil.clean", f"touch {marker}")
    git(repository, "config", "core.fsmonitor", f"touch {marker}")
    (repository / "Proof.lean").write_text("staged version\n")
    # Disable untrusted helpers while constructing the staged fixture.
    git(repository, "-c", "core.fsmonitor=false", "add", "Proof.lean")
    (repository / "Proof.lean").write_text("unstaged version\n")
    (repository / ".gitattributes").write_text("*.lean filter=evil\n")
    recovery = GitRecovery(repository, "repo-1", "execution-1", 1, journal)
    dirty = recovery.checkpoint_dirty()
    staged = git(repository, "rev-parse", "refs/horizon/staged/execution-1")
    assert git(repository, "show", staged + ":Proof.lean") == "staged version"
    assert git(repository, "show", dirty + ":Proof.lean") == "unstaged version"
    assert not marker.exists()


def test_git_publication_ignores_agent_url_rewrites(repository, journal, tmp_path):
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(bare))
    git(repository, "config", "url.ext::false.insteadOf", str(bare))
    git(repository, "config", "protocol.ext.allow", "always")
    recovery = GitRecovery(repository, "repo-1", "execution-1", 1, journal)
    oid = git(repository, "rev-parse", "HEAD")
    ref = recovery.preserve_remote(oid, remote=str(bare))
    assert git(bare, "rev-parse", ref) == oid


@pytest.mark.parametrize("remote", ["https://forge.invalid/project.git", "http://127.0.0.1:3000/project.git",
                                    "http://[::1]:3000/project.git", "http://localhost:18767/project.git"])
def test_git_publication_header_is_scoped_environment_only(repository, journal, monkeypatch, remote):
    recovery = GitRecovery(repository, "repo-1", "execution-1", 1, journal,
                           publication_header="Authorization: token scoped-secret")
    oid = git(repository, "rev-parse", "HEAD")
    ref = recovery.remote_prefix + oid
    calls = []

    def trusted(*args, env=None, **kwargs):
        calls.append((args, env))
        return f"{oid}\t{ref}\n" if len(calls) == 3 else ""

    monkeypatch.setattr(recovery, "_trusted", trusted)
    assert recovery.preserve_remote(oid, remote=remote) == ref
    assert all("scoped-secret" not in str(args) for args, env in calls)
    assert all(env["GIT_CONFIG_KEY_0"] == f"http.{remote}.extraHeader" for args, env in calls)
    assert all(env["GIT_CONFIG_VALUE_1"] == "false" for args, env in calls)
    assert b"scoped-secret" not in journal.path.read_bytes()


@pytest.mark.parametrize("remote", ["http://forge.invalid/project.git", "http://localhost.evil.invalid/project.git",
    "http://127.0.0.1.evil.invalid/project.git", "http://127.0.0.2/project.git", "http://127.1/project.git",
    "http://2130706433/project.git", "http://[::ffff:127.0.0.1]/project.git", "http://localhost@evil.invalid/project.git",
    "http://user:password@localhost/project.git", "https://user:password@forge.invalid/project.git",
    "https:///project.git", "http://localhost/project.git?token=secret", "http://localhost/project.git#fragment"])
def test_git_publication_header_rejects_remote_http_and_credential_urls(repository, journal, monkeypatch, remote):
    recovery = GitRecovery(repository, "repo-1", "execution-1", 1, journal,
                           publication_header="Authorization: token scoped-secret")
    oid = git(repository, "rev-parse", "HEAD")
    monkeypatch.setattr(recovery, "_trusted", lambda *args, **kwargs: pytest.fail("unsafe authenticated transport"))
    with pytest.raises(ValueError, match="authorization headers require"):
        recovery.preserve_remote(oid, remote=remote)


def test_headless_adapter_resumes_exact_native_thread():
    codex = HeadlessAdapter("codex_exec", "/bin/codex", model="test-model")
    command = codex.command(provider_thread_id="thread-123")
    assert command[:4] == ["/bin/codex", "exec", "resume", "thread-123"]
    assert "--last" not in command
    assert command[-1] == "-"
    claude = HeadlessAdapter("claude_exec", "/bin/claude", sandbox_mode="read_only")
    command = claude.command(provider_thread_id="thread-123")
    assert command[command.index("--resume") + 1] == "thread-123"
    assert "--print" in command


@pytest.mark.parametrize("thread_id", [None, "thread-123"])
def test_codex_workspace_write_grants_only_assignment_intent_directory(tmp_path, thread_id):
    from dataclasses import replace

    codex = HeadlessAdapter("codex_exec", "/bin/codex")
    state = tmp_path / "provider home" / "assignments" / "assignment-1" / "api-intents"
    command = codex.command(provider_thread_id=thread_id, agent_state_path=state)
    roots = [arg.split("=", 1)[1] for arg in command if arg.startswith("sandbox_workspace_write.writable_roots=")]
    assert len(roots) == 1 and json.loads(roots[0]) == [str(state)]
    if thread_id:
        assert command[:4] == ["/bin/codex", "exec", "resume", thread_id]
    else:
        assert command[:2] == ["/bin/codex", "exec"] and "resume" not in command
    assert not any(arg.startswith("sandbox_workspace_write.writable_roots=") for arg in codex.command())
    for path in (Path("relative"), Path("/"), Path("/state/../other")):
        with pytest.raises(ValueError, match="explicit absolute"):
            codex.command(agent_state_path=path)
    readonly = replace(codex, sandbox_mode="read_only")
    isolated = replace(codex, sandbox_mode="externally_isolated", approval_mode="preauthorized")
    for adapter, external in ((readonly, False), (isolated, True),
                              (HeadlessAdapter("claude_exec", "/bin/claude", sandbox_mode="read_only"), False)):
        assert not any(arg.startswith("sandbox_workspace_write.writable_roots=")
                       for arg in adapter.command(externally_isolated=external))
        with pytest.raises(ValueError, match="only supported for host Codex"):
            adapter.command(agent_state_path=state, externally_isolated=external)


@pytest.mark.parametrize("thread_id", [None, "thread-123"])
def test_default_codex_delegation_is_enabled_without_a_horizon_thread_cap(thread_id):
    command = HeadlessAdapter("codex_exec", "/bin/codex").command(provider_thread_id=thread_id)
    assert "agents.enabled=true" in command
    assert "features.multi_agent=true" in command
    assert "features.multi_agent_v2=true" in command
    assert 'web_search="live"' in command
    assert not any(arg.startswith("agents.max_threads=") for arg in command)


@pytest.mark.parametrize("thread_id", [None, "thread-123"])
def test_uncapped_claude_keeps_native_tools_and_explicit_disabling_removes_delegation(thread_id):
    from dataclasses import replace

    claude = HeadlessAdapter("claude_exec", "/bin/claude", sandbox_mode="externally_isolated")
    command = claude.command(externally_isolated=True, provider_thread_id=thread_id)
    assert command[command.index("--tools") + 1] == "default"
    assert not {"--setting-sources", "--strict-mcp-config", "--mcp-config", "--bare", "--safe-mode"} & set(command)
    assert command[command.index("--autocompact") + 1] == "auto"
    disabled = replace(claude, max_parallel_subagents=0).command(externally_isolated=True)
    assert disabled[disabled.index("--tools") + 1] == "default"
    assert set(disabled[disabled.index("--disallowedTools") + 1].split(",")) == {"Agent", "Task"}
    with pytest.raises(ValueError, match="unsupported Claude tool"):
        replace(claude, max_parallel_subagents=0, tool_names=("Agent",)).command(externally_isolated=True)


@pytest.mark.parametrize("thread_id", [None, "thread-123"])
def test_native_feature_overrides_remain_explicit_on_start_and_resume(thread_id):
    codex = HeadlessAdapter("codex_exec", "/bin/codex", codex_multi_agent_v2=False, codex_web_search="cached")
    command = codex.command(provider_thread_id=thread_id)
    assert "features.multi_agent_v2=false" in command and 'web_search="cached"' in command
    claude = HeadlessAdapter("claude_exec", "/bin/claude", sandbox_mode="externally_isolated",
                             claude_native_configuration=False)
    command = claude.command(provider_thread_id=thread_id, externally_isolated=True)
    assert command[command.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in command
    assert json.loads(command[command.index("--mcp-config") + 1]) == {"mcpServers": {}}


def test_claude_tool_selection_accepts_native_catalog_extensions_and_helpers_can_finish():
    claude = HeadlessAdapter("claude_exec", "/bin/claude", sandbox_mode="externally_isolated",
        tool_names=("NotebookEdit", "Skill", "TaskCreate", "ToolSearch"))
    command = claude.command(externally_isolated=True)
    assert command[command.index("--tools") + 1] == "NotebookEdit,Skill,TaskCreate,ToolSearch"
    assert claude.runtime_environment() == {"CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "0"}
    assert HeadlessAdapter("codex_exec", "/bin/codex").runtime_environment() == {}


@pytest.mark.parametrize("settings", [{"codex_multi_agent_v2": 1}, {"claude_native_configuration": "true"},
                                      {"codex_web_search": "unknown"}])
def test_native_feature_settings_reject_malformed_values(settings):
    with pytest.raises(ValueError):
        HeadlessAdapter("codex_exec", "/bin/codex", **settings).command()


@pytest.mark.parametrize("limit", [-1, True, 1.5, "2"])
def test_malformed_subagent_limits_are_rejected_by_worker_contracts(tmp_path, limit):
    with pytest.raises(ValueError, match="nonnegative integer"):
        HeadlessAdapter("codex_exec", "/bin/codex", max_parallel_subagents=limit).command()
    with pytest.raises(ValueError, match="nonnegative integer"):
        ExecutionGrant("execution", "assignment", 1, 60, "harness", "workspace",
                       str(tmp_path), "repo", "Goal", max_parallel_subagents=limit)


def test_headless_settings_are_enforced_or_rejected():
    from dataclasses import replace

    codex = HeadlessAdapter("codex_exec", "/bin/codex", max_parallel_subagents=3)
    command = codex.command()
    assert 'approval_policy="never"' in command
    assert 'sandbox_mode="workspace-write"' in command
    assert "agents.max_threads=3" in command
    assert "agents.enabled=true" in command
    assert "features.multi_agent=true" in command
    assert "features.multi_agent_v2=true" in command
    assert "--dangerously-bypass-approvals-and-sandbox" not in command
    assert "agents.enabled=false" in replace(codex, max_parallel_subagents=0).command()
    assert "features.multi_agent=false" in replace(codex, max_parallel_subagents=0).command()
    assert "features.multi_agent_v2=false" in replace(codex, max_parallel_subagents=0).command()
    for changes in ({"tool_names": ("Bash",)}, {"auto_compaction": False},
                    {"approval_mode": "automatic_review"}, {"approval_mode": "preauthorized"}):
        with pytest.raises(ValueError):
            replace(codex, **changes).command()
    isolated = replace(codex, approval_mode="preauthorized", sandbox_mode="externally_isolated")
    with pytest.raises(ValueError, match="Podman"):
        isolated.command()
    assert "--dangerously-bypass-approvals-and-sandbox" in isolated.command(externally_isolated=True)
    claude = HeadlessAdapter("claude_exec", "/bin/claude", sandbox_mode="read_only")
    command = claude.command()
    assert command[command.index("--tools") + 1] == "Read,Glob,Grep"
    assert command[command.index("--permission-mode") + 1] == "dontAsk"
    assert "--strict-mcp-config" in command
    for changes in ({"tool_names": ("Bash",)}, {"sandbox_mode": "workspace_write"},
                    {"max_parallel_subagents": 1}):
        with pytest.raises(ValueError):
            replace(claude, **changes).command()


def test_supervisor_continuation_and_no_duplicate_uncertain_request(journal, repository):
    journal.grant_lease("execution-1", 1, 10)
    supervisor = ProcessSupervisor(journal)
    command = [sys.executable, "-c", "import json,sys; p=sys.stdin.read(); print(json.dumps({'type':'thread.started','thread_id':'thread-123'})); print(json.dumps({'type':'turn.completed'})); print(p)"]
    first = supervisor.run(command, prompt="Continue", request_id="request-1", execution_id="execution-1", epoch=1,
                           workspace=repository, env={"PATH": os.defpath})
    assert first.status == "succeeded"
    assert first.provider_thread_id == "thread-123"
    duplicate = supervisor.run(["/nonexistent"], prompt="Continue", request_id="request-1", execution_id="execution-1", epoch=1,
                               workspace=repository, env={"PATH": os.defpath})
    assert duplicate == first
    journal.begin_request("uncertain-request", "execution-1", 1, "maybe delivered")
    with pytest.raises(FencedExecution, match="uncertain"):
        supervisor.run(command, prompt="maybe delivered", request_id="uncertain-request", execution_id="execution-1", epoch=1,
                       workspace=repository, env={"PATH": os.defpath})


def test_supervisor_deadline_is_a_local_execution_failure(journal, repository):
    journal.grant_lease("execution-1", 1, 10)
    supervisor = ProcessSupervisor(journal, poll_seconds=0.01, max_request_seconds=0.15)
    result = supervisor.run([sys.executable, "-c", "import time; time.sleep(30)"],
        prompt="", request_id="request-deadline", execution_id="execution-1", epoch=1,
        workspace=repository, env={"PATH": os.defpath})
    assert result.status == "failed" and result.reason == "request_deadline"
    assert result.failure["kind"] == "execution" and result.failure["code"] == "request_deadline"
    assert json.loads(journal.request("request-deadline")["result"])["failure"] == result.failure


def test_worker_execution_budget_is_positive_and_separate_from_request_budget(journal, tmp_path):
    config = HarnessConfig(HeadlessAdapter("codex_exec", "/codex"),
                           tmp_path / "provider", tmp_path / "scratch", unrestricted=True)
    with pytest.raises(ValueError, match="execution budget"):
        WorkerDaemon(host_id="host", journal=journal, transport=WorkerTransport("http://testserver", "token"),
                     harnesses={"harness": config}, workspace_roots=(tmp_path,), max_execution_seconds=0)
    daemon = WorkerDaemon(host_id="host", journal=journal, transport=WorkerTransport("http://testserver", "token"),
                          harnesses={"harness": config}, workspace_roots=(tmp_path,),
                          max_request_seconds=3600, max_execution_seconds=7200)
    assert daemon.supervisor.max_request_seconds == 3600
    assert daemon.max_execution_seconds == 7200


def test_supervisor_stops_on_lease_expiry_and_distinguishes_cancel(journal, repository):
    supervisor = ProcessSupervisor(journal, poll_seconds=0.01)
    command = [sys.executable, "-c", 'import time; print(\'{"type":"ready"}\', flush=True); time.sleep(30)']
    journal.grant_lease("execution-1", 1, 60)
    # Expire after native startup, so slow journal fsync cannot expire the lease
    # before the process whose termination this test needs to observe exists.
    def expire_after_start(event):
        if event.get("type") == "ready":
            journal.grant_lease("execution-1", 1, 0.001)
    result = supervisor.run(command, prompt="", request_id="request-expired", execution_id="execution-1", epoch=1,
                            workspace=repository, env={"PATH": os.defpath}, on_event=expire_after_start)
    assert result.status == "lost"
    assert result.reason == "lease_expired"
    journal.grant_lease("execution-2", 1, 60)
    cancel = threading.Event()
    cancel.set()
    result = supervisor.run(command, prompt="", request_id="request-cancel", execution_id="execution-2", epoch=1,
                            workspace=repository, env={"PATH": os.defpath}, cancel=cancel)
    assert result.status == "cancelled"


def test_sandbox_denies_sensitive_mounts_and_uses_disk_scratch(tmp_path):
    workspace, provider, scratch, state = (tmp_path / name for name in ("workspace", "provider", "scratch", "state"))
    for path in (workspace, provider, scratch, state):
        path.mkdir()
    policy = SandboxPolicy("registry.invalid/worker@sha256:" + "a" * 64)
    args = podman_command(policy, workspace=workspace, provider_home=provider, scratch=scratch,
                          protected_roots=(state,), command=["codex", "exec", "-"], name="horizon-test", uid=1000, gid=1000)
    assert "--read-only" in args and "--cap-drop=ALL" in args
    assert any(f"src={scratch},dst=/tmp" in arg for arg in args)
    assert any(f"src={scratch},dst=/var/tmp" in arg for arg in args)
    assert "--privileged" not in args
    unsafe = SandboxPolicy(policy.image_digest, extra_mounts=(SandboxMount(tmp_path, "/unsafe", False),))
    with pytest.raises(ValueError, match="protected"):
        podman_command(unsafe, workspace=workspace, provider_home=provider, scratch=scratch,
                       protected_roots=(state,), command=["true"], name="horizon-test", uid=1000, gid=1000)


def test_orchestrator_tool_guard_rejects_repository_and_build_actions():
    assert WorkerDaemon._orchestrator_tool_violation({
        "item": {"type": "file_change", "path": "Main.lean"}
    })


@pytest.mark.parametrize("command,violates", [
    ("lake build", True),
    ("git status && lean Main.lean", True),
    ("/bin/bash -lc 'lake build'", True),
    ("/home/axel/.elan/bin/lake build", True),
    ("cd /workspace && /home/axel/.elan/bin/lean Main.lean", True),
    ("echo starting\nlake build", True),
    ("env LEAN_PATH=/workspace command -- lake build", True),
    ("/bin/bash -lc \"/bin/sh -c 'git status'\"", True),
    ("curl -s http://horizon/api/v3/runs", False),
    ("printf '%s' 'do not run git status'", False),
    ("echo 'lake build && git status'", False),
    ("/bin/bash -lc \"printf '%s' 'lake build'\"", False),
    ("horizon-pipeline agent request POST /api/v3/commands '{\"note\":\"lake build stalled\"}'", False),
    ("/bin/bash -lc 'unterminated", False),
])
def test_orchestrator_tool_guard_parses_executables_in_shell_commands(command, violates):
    assert WorkerDaemon._orchestrator_tool_violation({
        "item": {"type": "command_execution", "command": command}
    }) is violates


def test_orchestrator_tool_guard_bounds_nested_shell_parsing():
    import shlex
    command = "lake build"
    for _ in range(6):
        command = "bash -c " + shlex.quote(command)
    assert not WorkerDaemon._orchestrator_tool_violation({
        "item": {"type": "command_execution", "command": command}
    })


@pytest.mark.parametrize("orchestrator", [True, False])
def test_shared_harness_disables_native_fanout_only_for_orchestrators(journal, repository, tmp_path, orchestrator):
    binary = tmp_path / "codex"
    binary.write_text("#!" + sys.executable + "\nimport json,sys\n"
        f"assert ('agents.enabled=false' in sys.argv) is {orchestrator!r}\n"
        f"assert ('features.multi_agent=false' in sys.argv) is {orchestrator!r}\n"
        f"assert ('agents.max_threads=2' in sys.argv) is {not orchestrator!r}\n"
        "print(json.dumps({'type':'thread.started','thread_id':'thread-123'}))\n"
        "print(json.dumps({'type':'turn.completed'}))\n")
    binary.chmod(0o700)
    grant = ExecutionGrant("execution-1", "assignment-1", 1, 60, "harness-1", "workspace-1",
        str(repository), "repo-1", "Observe Horizon" if orchestrator else "Work on mission",
        execution_token="execution-secret", functions=("orchestrator",) if orchestrator else (),
        max_parallel_subagents=2)

    def handler(request):
        if request.url.path.endswith("/claim"):
            return httpx.Response(200, json={"execution": grant.__dict__})
        if request.url.path.endswith("/reviewer-accounts"):
            return httpx.Response(200, json={"execution_id": grant.execution_id, "accounts": []})
        if request.url.path.endswith("/heartbeat"):
            return httpx.Response(200, json={"lease_seconds": 60, "continue": False})
        return httpx.Response(200, json={"acknowledged": True})

    transport = WorkerTransport("http://testserver", "host-secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)))
    adapter = HeadlessAdapter("codex_exec", str(binary), max_parallel_subagents=2)
    config = HarnessConfig(adapter, tmp_path / "provider", tmp_path / "scratch", unrestricted=True)
    daemon = WorkerDaemon(host_id="host-1", journal=journal, transport=transport,
        harnesses={"harness-1": config}, workspace_roots=(repository,))
    try:
        assert daemon.run_once() == "succeeded"
        assert config.adapter.max_parallel_subagents == 2
    finally:
        transport.close()


@pytest.mark.parametrize("remote_code,expected", [(None, None),
    ("request_timeout", "timeout"), ("rate_limit_exceeded", "rate_limited")])
def test_daemon_completion_carries_normalized_provider_failure(journal, repository, tmp_path, remote_code, expected):
    binary = tmp_path / "codex"
    event = {"type": "turn.failed", "error": {"code": remote_code, "message": "Sensitive upstream credential"}} if remote_code else {
        "type": "turn.completed"}
    binary.write_text("#!" + sys.executable + "\nimport json\n"
        "print(json.dumps({'type':'thread.started','thread_id':'thread-123'}))\n"
        "print(" + repr(json.dumps(event)) + ")\n")
    binary.chmod(0o700)
    grant = ExecutionGrant("execution-1", "assignment-1", 1, 60, "harness-1", "workspace-1",
        str(repository), "repo-1", "Work on mission", execution_token="execution-secret")
    observations = []

    def handler(request):
        if request.url.path.endswith("/claim"):
            return httpx.Response(200, json={"execution": grant.__dict__})
        if request.url.path.endswith("/reviewer-accounts"):
            return httpx.Response(200, json={"execution_id": grant.execution_id, "accounts": []})
        if request.url.path.endswith("/heartbeat"):
            return httpx.Response(200, json={"lease_seconds": 60, "continue": False})
        observations.append(json.loads(request.content))
        return httpx.Response(200, json={"acknowledged": True})

    transport = WorkerTransport("http://testserver", "host-secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)))
    config = HarnessConfig(HeadlessAdapter("codex_exec", str(binary)),
        tmp_path / "provider", tmp_path / "scratch", unrestricted=True)
    daemon = WorkerDaemon(host_id="host-1", journal=journal, transport=transport,
        harnesses={"harness-1": config}, workspace_roots=(repository,))
    try:
        assert daemon.run_once() == ("failed" if remote_code else "succeeded")
        completed = next(row["payload"] for row in observations if row["payload"].get("event") == "request_completed")
        finished = next(row["payload"] for row in observations if row["kind"] == "execution_finished")
        if remote_code:
            assert completed["failure"] == finished["failure"]
            assert completed["failure"]["code"] == expected
            assert "Sensitive" not in completed["failure"]["message"]
            assert set(completed["failure"]) == {"kind", "code", "message"}
        else:
            assert "failure" not in completed and "failure" not in finished
    finally:
        transport.close()


@pytest.mark.parametrize("pending_intents,cleanup_failure", [(False, False), (True, False), (False, True)])
def test_daemon_continues_same_context_without_host_credential(journal, repository, tmp_path, monkeypatch, pending_intents, cleanup_failure):
    scans_with_heartbeat = []
    original_reconcile = GitRecovery.reconcile

    def reconcile_with_lease(recovery, **kwargs):
        scans_with_heartbeat.append(any(thread.name == "horizon-lease-renewal" and thread.is_alive()
                                       for thread in threading.enumerate()))
        return original_reconcile(recovery, **kwargs)

    monkeypatch.setattr(GitRecovery, "reconcile", reconcile_with_lease)
    tools = tmp_path / "tools"
    tools.mkdir()
    tool = tools / "horizon-test-tool"
    tool.write_text("#!/bin/sh\nprintf 'configured-tool-visible'\n")
    tool.chmod(0o755)
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-secret-not-for-provider")
    monkeypatch.setenv("HORIZON_HOST_TOKEN", "ambient-host-secret")
    script = tmp_path / "provider.py"
    script.write_text("import json,os,subprocess,sys\n"
                      "assert 'HORIZON_HOST_TOKEN' not in os.environ\n"
                      "assert 'OPENAI_API_KEY' not in os.environ\n"
                      "assert os.environ['HORIZON_EXECUTION_TOKEN']=='execution-secret'\n"
                      "assert os.environ['ELAN_HOME'].endswith('/toolchains')\n"
                      "from pathlib import Path\n"
                      "accounts = Path(os.environ['HORIZON_REVIEWER_ACCOUNTS_FILE'])\n"
                      "if str(accounts) != '/tmp/reviewer-accounts.json':\n"
                      " assert accounts.stat().st_mode & 0o777 == 0o600\n"
                      " assert json.loads(accounts.read_text())['accounts'][0]['token'] == 'reviewer-private'\n"
                      "assert 'reviewer-private' not in os.environ.values()\n"
                      "assert subprocess.check_output(['horizon-test-tool']) == b'configured-tool-visible'\n"
                      "print(json.dumps({'type':'thread.started','thread_id':'thread-123'}))\n"
                      "print(json.dumps({'type':'turn.completed'}))\n")
    commands = []
    writable_roots = []

    class FakeAdapter:
        provider = "codex_exec"
        sandbox_mode = "workspace_write"

        def command(self, *, provider_thread_id=None, externally_isolated=False, agent_state_path=None):
            commands.append(provider_thread_id)
            writable_roots.append(agent_state_path)
            return [sys.executable, str(script)]

    observations = []
    decisions = [True, False]
    grant = ExecutionGrant("execution-1", "assignment-1", 1, 60, "harness-1", "workspace-1", str(repository), "repo-1", "Original mission", execution_token="execution-secret")

    def handler(request):
        if request.url.path.endswith("/reviewer-accounts"):
            assert request.headers["Authorization"] == "Bearer execution-secret"
            return httpx.Response(200, json={"execution_id": grant.execution_id, "accounts": [{"token": "reviewer-private"}]})
        body = json.loads(request.content)
        if request.url.path.endswith("/claim"):
            return httpx.Response(200, json={"execution": grant.__dict__})
        if request.url.path.endswith("/heartbeat"):
            return httpx.Response(200, json={"lease_seconds": 60, "stop": False,
                                            "continue": decisions.pop(0), "goal": "Continue only unresolved work",
                                            "mission_revision_id": "new-revision", "mission_revision_number": 2,
                                            "run_revision": 2})
        observations.append(body)
        return httpx.Response(200, json={"acknowledged": True})

    transport = WorkerTransport("http://testserver", "host-secret", client=httpx.Client(transport=httpx.MockTransport(handler)))
    config = HarnessConfig(FakeAdapter(), tmp_path / "provider-home", tmp_path / "scratch", unrestricted=True,
                           environment={"PATH": str(tools) + ":" + os.defpath,
                                        "ELAN_HOME": str(tmp_path / "toolchains")})
    if cleanup_failure:
        from dataclasses import replace
        config = replace(config, sandbox=SandboxPolicy("test@sha256:" + "a" * 64))
        monkeypatch.setattr(WorkerDaemon, "_podman_executable", staticmethod(lambda: "/fake/podman"))
        monkeypatch.setattr("archon_horizon.pipeline.worker.daemon.podman_command", lambda *args, **kwargs: [
            sys.executable, "-c", "import json,os,sys; os.environ.update(json.loads(sys.argv[1])); os.execv(sys.argv[2],sys.argv[2:])",
            json.dumps(kwargs["environment_values"]), *kwargs["command"]])
    daemon = WorkerDaemon(host_id="host-1", journal=journal, transport=transport,
                          harnesses={"harness-1": config}, workspace_roots=(repository,))
    if pending_intents:
        monkeypatch.setattr(daemon, "_replay_agent_intents", lambda *args: {"completed": [], "blocked": [], "pending": 1})
    if cleanup_failure:
        def failed_cleanup(name):
            raise RuntimeError("Podman unavailable while container may still run")
        monkeypatch.setattr(daemon, "_stop_container", failed_cleanup)
        with pytest.raises(RuntimeError, match="stop is unconfirmed"):
            daemon.run_once()
        assert not any(json.loads(row["envelope"])["kind"] == "execution_finished" for row in journal.records())
        assert journal.executions()[0]["checkpoint"]["recovery_pending"] is True
        assert scans_with_heartbeat and all(scans_with_heartbeat)
        assert writable_roots and all(root is None for root in writable_roots)
        return
    assert daemon.run_once() == ("yielded" if pending_intents else "succeeded")
    assert not (tmp_path / "scratch" / grant.execution_id / "reviewer-accounts.json").exists()
    assert b"reviewer-private" not in journal.path.read_bytes()
    assert scans_with_heartbeat and all(scans_with_heartbeat)
    native = [item for item in observations if item["payload"].get("event") == "native_event"]
    assert len(native) == (1 if pending_intents else 2)
    assert all(item["payload"]["raw"]["type"] == "turn.completed" for item in native)
    assert (tmp_path / "provider-home" / "assignments" / "assignment-1" / "api-intents").is_dir()
    assert commands == ([None] if pending_intents else [None, "thread-123"])
    assert writable_roots == [tmp_path / "provider-home" / "assignments" / "assignment-1" / "api-intents"] * len(commands)
    events = [row["payload"]["event"] for row in observations if row["kind"] == "provider_observed"]
    assert events == ["request_started", "native_event", "request_completed"] * (1 if pending_intents else 2)
    started = [row["payload"] for row in observations if row["kind"] == "provider_observed" and row["payload"]["event"] == "request_started"]
    assert started[0]["mission_revision_number"] == 1
    if not pending_intents:
        assert started[1]["mission_revision_number"] == 2
        assert started[1]["mission_revision_id"] == "new-revision"
    else:
        finished = [item["payload"] for item in observations if item["kind"] == "execution_finished"]
        assert finished[-1]["reason"] == "agent_intents_require_reconciliation"
    assert all("execution-secret" not in row["envelope"] for row in journal.records())
    assert journal.executions()[0]["status"] == ("yielded" if pending_intents else "succeeded")


def test_daemon_reports_unsupported_provider_configuration_without_launch(journal, repository, tmp_path):
    grant = ExecutionGrant("execution-1", "assignment-1", 1, 60, "harness-1", "workspace-1", str(repository), "repo-1", "Goal")
    observed = []

    def handler(request):
        if request.url.path.endswith("/claim"):
            return httpx.Response(200, json={"execution": grant.__dict__})
        observed.append(json.loads(request.content))
        return httpx.Response(200, json={"acknowledged": True})

    transport = WorkerTransport("http://testserver", "host-secret", client=httpx.Client(transport=httpx.MockTransport(handler)))
    config = HarnessConfig(HeadlessAdapter("claude_exec", "/must-not-launch"), tmp_path / "home", tmp_path / "scratch", unrestricted=True)
    daemon = WorkerDaemon(host_id="host-1", journal=journal, transport=transport,
                          harnesses={"harness-1": config}, workspace_roots=(repository,))
    assert daemon.run_once() == "failed"
    assert len(observed) == 1
    assert observed[0]["payload"]["failure"]["code"] == "invalid_configuration"
    assert "workspace_write" in observed[0]["payload"]["failure"]["message"]


def test_worker_rejects_harness_model_drift_before_launch(tmp_path):
    config = HarnessConfig(HeadlessAdapter("codex_exec", "/codex", model="different-model"),
                           tmp_path / "provider", tmp_path / "scratch", unrestricted=True,
                           provider_version="1", adapter_version="1")
    grant = ExecutionGrant("execution", "assignment", 1, 60, "harness", "workspace", str(tmp_path), "repo", "Goal",
                            harness_configuration={"adapter": "codex_exec", "model_options": {"model": "pinned-model"},
                                                   "provider_version": "1", "adapter_version": "1"})
    with pytest.raises(ValueError, match="model differs"):
        WorkerDaemon._validate_harness(grant, config)
    from dataclasses import replace
    configured = replace(config, allowed_models=("different-model", "pinned-model"))
    resolved = WorkerDaemon._validate_harness(grant, configured)
    assert resolved.model == "pinned-model"
    assert resolved.executable == config.adapter.executable
    assert config.adapter.model == "different-model"


def test_worker_applies_pinned_native_feature_choices_without_mutating_local_defaults(tmp_path):
    config = HarnessConfig(HeadlessAdapter("codex_exec", "/codex"), tmp_path / "provider",
                           tmp_path / "scratch", unrestricted=True)
    grant = ExecutionGrant("execution", "assignment", 1, 60, "harness", "workspace", str(tmp_path), "repo", "Goal",
        harness_configuration={"adapter": "codex_exec", "model_options": {}, "settings": {
            "codex_multi_agent_v2": False, "codex_web_search": "disabled", "claude_native_configuration": False}})
    resolved = WorkerDaemon._validate_harness(grant, config)
    assert "features.multi_agent_v2=false" in resolved.command()
    assert 'web_search="disabled"' in resolved.command()
    assert resolved.claude_native_configuration is False
    assert config.adapter.codex_multi_agent_v2 is True and config.adapter.codex_web_search == "live"


def test_worker_verifies_sandbox_attestation_and_actual_binary_version(journal, repository, tmp_path):
    from dataclasses import replace
    from archon_horizon.pipeline.worker.sandbox import SandboxMount

    binary = tmp_path / "codex"
    binary.write_text("#!" + sys.executable + "\nprint('codex-cli 1.2.3')\n")
    binary.chmod(0o700)
    config = HarnessConfig(HeadlessAdapter("codex_exec", str(binary)), tmp_path / "home", tmp_path / "scratch",
                           unrestricted=True, provider_version="1.2.3")
    transport = WorkerTransport("http://testserver", "host-secret")
    daemon = WorkerDaemon(host_id="host", journal=journal, transport=transport, harnesses={"harness": config}, workspace_roots=(repository,))
    try:
        daemon._validate_executable(config)
        binary.write_text("#!" + sys.executable + "\nprint('codex-cli 2.0.0')\n")
        with pytest.raises(ValueError, match="actual provider"):
            daemon._validate_executable(config)
    finally:
        transport.close()
    grant = ExecutionGrant("execution", "assignment", 1, 60, "harness", "workspace", str(repository), "repo", "Goal",
                            sandbox_manifest={"mode": "rootless_container", "image_digest": "image@sha256:" + "a" * 64})
    with pytest.raises(ValueError, match="sandbox mode"):
        WorkerDaemon._validate_harness(grant, config)
    sandbox = SandboxPolicy("image@sha256:" + "a" * 64)
    WorkerDaemon._validate_harness(grant, replace(config, sandbox=sandbox))
    with pytest.raises(ValueError, match="sandbox settings"):
        WorkerDaemon._validate_harness(grant, replace(config, sandbox=replace(sandbox, network="none")))
    with pytest.raises(ValueError, match="sandbox settings"):
        WorkerDaemon._validate_harness(grant, replace(config, sandbox=replace(sandbox,
            extra_mounts=(SandboxMount(tmp_path, "/extra", False),))))


def test_daemon_runs_verified_provider_from_explicit_tool_path(journal, repository, tmp_path, monkeypatch):
    tools = tmp_path / "custom-tools"
    tools.mkdir()
    binary = tools / "codex"
    binary.write_text("#!" + sys.executable + "\nimport json,sys\nfrom pathlib import Path\n"
                      "if '--version' in sys.argv:\n print('codex-cli 1.2.3')\nelse:\n"
                      " Path('provider-path.txt').write_text(str(Path(sys.argv[0]).resolve()))\n"
                      " print(json.dumps({'type':'thread.started','thread_id':'thread-123'}))\n"
                      " print(json.dumps({'type':'turn.completed'}))\n")
    binary.chmod(0o700)
    monkeypatch.setenv("PATH", os.defpath)
    grant = ExecutionGrant("execution", "assignment", 1, 60, "harness", "workspace", str(repository), "repo", "Goal")
    def respond(request):
        if request.url.path.endswith("/claim"):
            return httpx.Response(200, json={"execution": grant.__dict__})
        if request.url.path.endswith("/heartbeat"):
            return httpx.Response(200, json={"lease_seconds": 60, "continue": False})
        return httpx.Response(200, json={"acknowledged": True})
    transport = WorkerTransport("http://testserver", "host-secret", client=httpx.Client(transport=httpx.MockTransport(respond)))
    config = HarnessConfig(HeadlessAdapter("codex_exec", "codex"), tmp_path / "home", tmp_path / "scratch",
                           unrestricted=True, provider_version="1.2.3", environment={"PATH": str(tools)})
    daemon = WorkerDaemon(host_id="host", journal=journal, transport=transport,
                          harnesses={"harness": config}, workspace_roots=(repository,))
    try:
        assert daemon._validate_executable(config) == str(binary.resolve())
        assert daemon.run_once() == "succeeded"
        assert (repository / "provider-path.txt").read_text() == str(binary.resolve())
    finally:
        transport.close()


@pytest.mark.parametrize("provider", ["codex_exec", "claude_exec"])
def test_daemon_keeps_host_podman_environment_separate_from_image_tools(journal, repository, tmp_path, monkeypatch, provider):
    host_bin = tmp_path / "host-bin"
    host_bin.mkdir()
    host_home = tmp_path / "host-home"
    host_home.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    binary = host_bin / "podman"
    events = [{"type": "system", "subtype": "init", "session_id": "thread-123"},
              {"type": "result", "subtype": "success", "result": "Done"}] if provider == "claude_exec" else [
              {"type": "thread.started", "thread_id": "thread-123"}, {"type": "turn.completed"}]
    binary.write_text("#!" + sys.executable + "\nimport json,os,sys\nfrom pathlib import Path\n"
                      "if sys.argv[1] == 'rm': sys.exit(0)\n"
                      "entries=[sys.argv[i+1] for i,v in enumerate(sys.argv) if v=='--env']\n"
                      "assert 'PATH=/image-tools' in entries\n"
                      "assert 'XDG_CACHE_HOME=/image-cache' in entries\n"
                      "assert 'HOME=/provider-home' in entries\n"
                      "assert 'HORIZON_REVIEWER_ACCOUNTS_FILE' in entries\n"
                      f"assert ('CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS' in entries) is {provider == 'claude_exec'!r}\n"
                      f"assert os.environ.get('CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS') == {('0' if provider == 'claude_exec' else None)!r}\n"
                      "assert Path(os.environ['HOME']).name=='host-home'\n"
                      "assert Path(os.environ['XDG_RUNTIME_DIR']).name=='runtime'\n"
                      "assert os.environ['PATH'].split(':')[0]==str(Path(sys.argv[0]).parent)\n"
                      "assert 'OPENAI_API_KEY' not in os.environ\n"
                      "assert not any('execution-secret' in value for value in sys.argv)\n"
                      f"for event in {events!r}: print(json.dumps(event))\n")
    binary.chmod(0o700)
    monkeypatch.setenv("PATH", str(host_bin) + ":" + os.defpath)
    monkeypatch.setenv("HOME", str(host_home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-secret")
    grant = ExecutionGrant("execution", "assignment", 1, 60, "harness", "workspace", str(repository), "repo", "Goal",
                           execution_token="execution-secret")
    def respond(request):
        if request.url.path.endswith("/reviewer-accounts"):
            return httpx.Response(200, json={"execution_id": grant.execution_id, "accounts": []})
        if request.url.path.endswith("/claim"):
            return httpx.Response(200, json={"execution": grant.__dict__})
        if request.url.path.endswith("/heartbeat"):
            return httpx.Response(200, json={"lease_seconds": 60, "continue": False})
        return httpx.Response(200, json={"acknowledged": True})
    transport = WorkerTransport("http://testserver", "host-secret", client=httpx.Client(transport=httpx.MockTransport(respond)))
    config = HarnessConfig(HeadlessAdapter(provider, "/image-tools/provider", approval_mode="preauthorized",
                                           sandbox_mode="externally_isolated"),
                           tmp_path / "provider", tmp_path / "scratch", sandbox=SandboxPolicy("image@sha256:" + "a" * 64),
                           environment={"PATH": "/image-tools", "XDG_CACHE_HOME": "/image-cache"})
    daemon = WorkerDaemon(host_id="host", journal=journal, transport=transport,
                          harnesses={"harness": config}, workspace_roots=(repository,))
    try:
        assert daemon.run_once() == "succeeded"
    finally:
        transport.close()


@pytest.mark.parametrize("failure", ["provider_home", "workspace_alias", "intent_directory_alias"])
def test_execution_initialization_fails_without_stranding_claim(journal, repository, tmp_path, failure):
    import fcntl
    import hashlib

    grant = ExecutionGrant("execution", "assignment", 1, 60, "harness", "workspace", str(repository), "repo", "Goal")
    journal.grant_lease(grant.execution_id, grant.epoch, 60)
    config = HarnessConfig(HeadlessAdapter("codex_exec", "/not-launched"), tmp_path / "home", tmp_path / "scratch", unrestricted=True)
    transport = WorkerTransport("http://testserver", "host-secret", client=httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"acknowledged": True}))))
    daemon = WorkerDaemon(host_id="host", journal=journal, transport=transport, harnesses={"harness": config}, workspace_roots=(repository,))
    lock = None
    if failure == "provider_home":
        config.provider_home.write_text("not a directory")
    elif failure == "intent_directory_alias":
        assignment = config.provider_home / "assignments" / grant.assignment_id
        assignment.mkdir(parents=True)
        sensitive = config.provider_home / ".codex"
        sensitive.mkdir()
        (assignment / "api-intents").symlink_to(sensitive, target_is_directory=True)
    else:
        locks = journal.state_root / "workspace-locks"
        locks.mkdir()
        lock = (locks / hashlib.sha256(str(repository.resolve()).encode()).hexdigest()).open("a")
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert daemon.execute(grant) == "failed"
    finally:
        if lock:
            lock.close()
    finished = [json.loads(row["envelope"]) for row in journal.records() if json.loads(row["envelope"])["kind"] == "execution_finished"]
    assert len(finished) == 1 and finished[0]["payload"]["failure"]["code"] == "invalid_configuration"
    assert not journal.executions()[0]["checkpoint"].get("recovery_pending")


def test_publication_retries_after_execution_finished_and_survives_restart(repository, journal, tmp_path, monkeypatch):
    remote = tmp_path / "offline.git"
    recovery = GitRecovery(repository, "repo-1", "execution-1", 1, journal)
    oid = git(repository, "rev-parse", "HEAD")
    recovery.reconcile()
    config = HarnessConfig(HeadlessAdapter("codex_exec", "/missing-provider"), tmp_path / "provider", tmp_path / "scratch", unrestricted=True)
    transport = WorkerTransport("http://testserver", "host-secret", client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))))
    daemon = WorkerDaemon(host_id="host-1", journal=journal, transport=transport,
                          harnesses={"harness-1": config}, workspace_roots=(repository,),
                          publication_remotes={"repo-1": str(remote)})
    with monkeypatch.context() as patch:
        def unavailable(*args, **kwargs):
            raise subprocess.TimeoutExpired("git push", 30)
        patch.setattr(GitRecovery, "preserve_remote", unavailable)
        assert daemon.preserve_one() == "pending"
    job = next(row for row in journal.records() if row["destination"] == "local_git")
    git(tmp_path, "init", "--bare", str(remote))
    assert daemon.preserve_one(now=job["retry_at"] + 1) == "acknowledged"
    assert git(remote, "rev-parse", recovery.remote_prefix + oid) == oid
    verified = [json.loads(row["envelope"]) for row in journal.records() if json.loads(row["envelope"])["kind"] == "publication_verified"]
    assert len(verified) == 1
    assert verified[0]["payload"]["commit_oid"] == oid


def test_daemon_restart_kills_only_recorded_process_and_recovers_dirty_work(journal, repository, tmp_path):
    config = HarnessConfig(HeadlessAdapter("codex_exec", "/missing-provider"), tmp_path / "provider", tmp_path / "scratch", unrestricted=True)
    transport = WorkerTransport("http://testserver", "host-secret", client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))))
    daemon = WorkerDaemon(host_id="host-1", journal=journal, transport=transport,
                          harnesses={"harness-1": config}, workspace_roots=(repository,))
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        journal.grant_lease("execution-1", 1, 60)
        journal.checkpoint("execution-1", 1, {"pid": process.pid, "process_identity": process_identity(process.pid),
                                            "boot_id": boot_identity(), "workspace_path": str(repository),
                                            "repository_id": "repo-1", "recovery_pending": True})
        (repository / "Recovered.lean").write_text("unpublished work")
        assert daemon.recover() == ["execution-1"]
        assert process.wait(timeout=5) < 0
        assert daemon.recover() == []
        assert journal.executions()[0]["status"] == "lost"
        assert any(json.loads(row["envelope"])["kind"] == "publication_discovered" for row in journal.records())
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()


def publication_daemon(repository, journal, tmp_path, *, handler=None, **options):
    config = HarnessConfig(HeadlessAdapter("codex_exec", "/unused"), tmp_path / "home",
                           tmp_path / "scratch", unrestricted=True)
    transport = WorkerTransport("http://testserver", "host-secret", client=httpx.Client(
        transport=httpx.MockTransport(handler or (lambda r: httpx.Response(200)))))
    return WorkerDaemon(host_id="host", journal=journal, transport=transport,
                        harnesses={"harness": config}, workspace_roots=(repository,), **options)


def test_cumulative_execution_budget_yields_with_durable_handoff(repository, journal, tmp_path):
    class Adapter:
        provider = "codex_exec"

        def command(self, **kwargs):
            raise AssertionError("provider must not start after the cumulative budget expires")

    grant = ExecutionGrant("execution-1", "assignment-1", 1, 60, "harness", "workspace",
                           str(repository), "repo-1", "Continue", execution_token="execution-secret")
    receipts = []

    def handler(request):
        if request.url.path.endswith("/claim"):
            return httpx.Response(200, json={"execution": grant.__dict__})
        if request.url.path.endswith("/reviewer-accounts"):
            return httpx.Response(200, json={"execution_id": grant.execution_id, "accounts": []})
        if request.url.path.endswith("/heartbeat"):
            return httpx.Response(200, json={"lease_seconds": 60, "continue": False})
        receipts.append(json.loads(request.content))
        return httpx.Response(200, json={"acknowledged": True})

    daemon = publication_daemon(repository, journal, tmp_path, handler=handler,
                                max_execution_seconds=0.001)
    daemon.harnesses["harness"] = HarnessConfig(Adapter(), tmp_path / "home", tmp_path / "scratch", unrestricted=True)
    assert daemon.run_once() == "yielded"
    finished = [item for item in receipts if item["kind"] == "execution_finished"]
    assert len(finished) == 1
    assert finished[0]["payload"]["status"] == "yielded"
    assert finished[0]["payload"]["reason"] == "execution_budget_reached"
    row = journal.executions()[0]
    assert row["status"] == "yielded"
    assert row["checkpoint"]["recovery_pending"] is False
    assert row["checkpoint"]["terminal_operation"]["payload"]["reason"] == "execution_budget_reached"


def test_execution_budget_interrupts_continuations_and_preserves_work(repository, journal, tmp_path):
    class Adapter:
        provider = "codex_exec"

        def command(self, **kwargs):
            return [sys.executable, "-c", "import json,pathlib,time; "
                    "pathlib.Path('Proof.lean').write_text('valuable unfinished work'); "
                    "print(json.dumps({'type':'thread.started','thread_id':'retained-context'}), flush=True); "
                    "time.sleep(.2); print(json.dumps({'type':'turn.completed'}), flush=True)"]

    grant = ExecutionGrant("execution-1", "assignment-1", 1, 60, "harness", "workspace",
                           str(repository), "repo-1", "Continue", execution_token="execution-secret")
    receipts = []

    def handler(request):
        if request.url.path.endswith("/claim"):
            return httpx.Response(200, json={"execution": grant.__dict__})
        if request.url.path.endswith("/reviewer-accounts"):
            return httpx.Response(200, json={"execution_id": grant.execution_id, "accounts": []})
        if request.url.path.endswith("/heartbeat"):
            return httpx.Response(200, json={"lease_seconds": 60, "continue": True, "goal": "Continue"})
        receipts.append(json.loads(request.content))
        return httpx.Response(200, json={"acknowledged": True})

    daemon = publication_daemon(repository, journal, tmp_path, handler=handler,
                                max_execution_seconds=1, max_request_seconds=60)
    daemon.harnesses["harness"] = HarnessConfig(Adapter(), tmp_path / "home", tmp_path / "scratch", unrestricted=True)
    assert daemon.run_once() == "yielded"
    finished = [item for item in receipts if item["kind"] == "execution_finished"]
    assert len(finished) == 1
    assert finished[0]["payload"]["reason"] == "execution_budget_reached"
    assert finished[0]["payload"]["provider_thread_id"] == "retained-context"
    started = [item for item in receipts if item["payload"].get("event") == "request_started"]
    assert 1 <= len(started) < 16
    assert git(repository, "show", "refs/horizon/dirty/execution-1:Proof.lean") == "valuable unfinished work"
    assert journal.executions()[0]["checkpoint"]["recovery_pending"] is False


def test_transient_publication_retries_beyond_budget_and_evidence_horizon(repository, journal, tmp_path, monkeypatch):
    recovery = GitRecovery(repository, "repo-1", "execution-1", 1, journal)
    recovery.reconcile()
    daemon = publication_daemon(repository, journal, tmp_path,
        publication_remotes={"repo-1": "https://forge.invalid/repo.git"},
        handler=lambda r: httpx.Response(503))
    def unavailable(*args, **kwargs):
        raise subprocess.TimeoutExpired("git push", 30)
    monkeypatch.setattr(GitRecovery, "preserve_remote", unavailable)
    now = time.time() + 8 * 86400
    for attempt in range(25):
        assert daemon.preserve_one(now=now) == "pending"
        assert daemon.transport.replay_one(journal, now=now) == "pending"
        now += 301
    assert all(row["state"] == "pending" and row["attempts"] == 25 for row in journal.records())
    assert all(0 < row["retry_at"] - (now - 301) <= 300 for row in journal.records())


def test_blocked_publication_is_visible_repairable_and_receipt_is_stable(repository, journal, tmp_path):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))
    recovery = GitRecovery(repository, "repo-1", "execution-1", 1, journal)
    recovery.reconcile()
    daemon = publication_daemon(repository, journal, tmp_path)
    assert daemon.preserve_one() == "blocked"
    job = next(row for row in journal.records() if row["destination"] == "local_git")
    failure = next(json.loads(row["envelope"]) for row in journal.records()
                   if json.loads(row["envelope"])["kind"] == "publication_failed")
    assert failure["payload"]["failure_code"] == "publication_remote_not_configured"
    daemon.publication_remotes["repo-1"] = str(remote)
    journal.retry_publication(job["operation_id"], note="Configured intended remote")
    assert daemon.preserve_one() == "acknowledged"
    verified = next(json.loads(row["envelope"]) for row in journal.records()
                    if json.loads(row["envelope"])["kind"] == "publication_verified")
    assert verified["occurred_at"] == json.loads(job["envelope"])["occurred_at"]
    with pytest.raises(ValueError, match="only blocked"):
        journal.retry_publication(job["operation_id"])
    daemon._flush()
    assert journal.compact(acknowledged_before=time.time() + 1) > 0
    assert not journal.records()
    recovery.reconcile()
    assert daemon.preserve_one() == "acknowledged"
    replayed = next(json.loads(row["envelope"]) for row in journal.records()
                    if json.loads(row["envelope"])["kind"] == "publication_verified")
    assert replayed == verified


def test_execution_completion_cannot_overtake_publication_inventory(journal):
    discovered = Operation.create("execution-1", 1, "publication_discovered",
        {"repository_id": "repo-1", "commit_oid": "a" * 40, "recovery_ref": "refs/horizon/test"})
    journal.enqueue(discovered)
    claim = journal.claim()
    journal.settle(claim, "pending", retry_at=time.time() + 100)
    journal.enqueue(Operation.create("execution-1", 1, "execution_finished", {"status": "succeeded"}))
    assert journal.claim() is None
    claim = journal.claim(now=time.time() + 101)
    assert claim.operation == discovered
    journal.settle(claim, "blocked", error="credentials need repair")
    assert journal.claim() is None
    journal.retry_publication(discovered.operation_id)
    journal.settle(journal.claim(), "acknowledged")
    assert journal.claim().operation.kind == "execution_finished"


def test_physical_stop_evidence_survives_prolonged_outage(journal):
    finished = Operation.create("execution-1", 1, "execution_finished", {"status": "succeeded"})
    journal.enqueue(finished)
    transport = WorkerTransport("http://testserver", "host-secret", client=httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(503))))
    now = time.time() + 8 * 86400
    for attempt in range(25):
        assert transport.replay_one(journal, now=now) == "pending"
        now += 301
    assert journal.records()[0]["state"] == "pending"
    assert journal.records()[0]["attempts"] == 25


def test_private_checkpoint_skips_git_lock_and_does_not_modify_staged_index(repository, journal):
    recovery = GitRecovery(repository, "repo-1", "execution-1", 1, journal)
    (repository / "Proof.lean").write_text("staged content\n")
    git(repository, "add", "Proof.lean")
    index = repository / ".git" / "index"
    before = index.read_bytes()
    (repository / "Proof.lean").write_text("new working content\n")
    lock = index.with_suffix(".lock")
    lock.touch()
    with pytest.raises(BlockingIOError):
        recovery.checkpoint_dirty()
    assert index.read_bytes() == before
    lock.unlink()
    oid = recovery.checkpoint_dirty()
    assert git(repository, "show", oid + ":Proof.lean") == "new working content"
    assert index.read_bytes() == before


def test_periodic_checkpoint_records_dirty_work_and_health_without_provider_boundary(repository, journal, tmp_path):
    recovery = GitRecovery(repository, "repo-1", "execution-1", 1, journal)
    daemon = publication_daemon(repository, journal, tmp_path, checkpoint_seconds=0.01)
    (repository / "Proof.lean").write_text("work during long invocation\n")
    stopped, cancelled = threading.Event(), threading.Event()
    failures = []
    thread = threading.Thread(target=daemon._checkpoint_loop, args=(recovery, stopped, cancelled, failures))
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not journal.recovery_health() and time.monotonic() < deadline:
            time.sleep(0.01)
        health = journal.recovery_health()[0]
        assert health["succeeded_at"] and health["error"] is None
        oid = git(repository, "rev-parse", "refs/horizon/dirty/execution-1")
        assert git(repository, "show", oid + ":Proof.lean") == "work during long invocation"
        assert not failures and not cancelled.is_set()
    finally:
        stopped.set()
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_background_publisher_runs_while_execution_lane_is_occupied(repository, journal, tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))
    recovery = GitRecovery(repository, "repo-1", "execution-1", 1, journal)
    recovery.reconcile()
    daemon = publication_daemon(repository, journal, tmp_path,
        publication_remotes={"repo-1": str(remote)}, publication_poll_seconds=0.01)
    stop, occupied, published = threading.Event(), threading.Event(), threading.Event()
    def occupied_lane(*, cancel):
        occupied.set()
        assert cancel.wait(5)
    monkeypatch.setattr(daemon, "run_once", occupied_lane)
    def observe(request):
        if json.loads(request.content).get("kind") == "publication_verified":
            published.set()
        return httpx.Response(200)
    daemon.transport.client = httpx.Client(transport=httpx.MockTransport(observe))
    failures = []
    def serve():
        try:
            daemon.serve(stop, slots=1, poll_seconds=0.01)
        except BaseException as error:
            failures.append(error)
    thread = threading.Thread(target=serve)
    thread.start()
    try:
        assert occupied.wait(5)
        assert published.wait(5)
        oid = git(repository, "rev-parse", "HEAD")
        assert git(remote, "rev-parse", recovery.remote_prefix + oid) == oid
    finally:
        stop.set()
        thread.join(timeout=5)
    assert not thread.is_alive() and not failures


def test_long_provider_invocation_is_checkpointed_before_it_finishes(repository, journal, tmp_path):
    script = tmp_path / "provider.py"
    script.write_text("""import json, pathlib, subprocess, time
pathlib.Path('Proof.lean').write_text('unfinished proof during invocation\\n')
deadline = time.monotonic() + 5
while time.monotonic() < deadline:
    result = subprocess.run(['git', 'show-ref', '--verify', 'refs/horizon/dirty/execution-1'], capture_output=True)
    if result.returncode == 0:
        break
    time.sleep(0.01)
else:
    raise SystemExit('checkpoint did not run during provider execution')
print(json.dumps({'type': 'thread.started', 'thread_id': 'context-1'}))
print(json.dumps({'type': 'turn.completed'}))
""")
    class Adapter:
        provider = "codex_exec"
        def command(self, **kwargs):
            return [sys.executable, str(script)]
    grant = ExecutionGrant("execution-1", "assignment-1", 1, 60, "harness", "workspace",
                           str(repository), "repo-1", "Continue the proof")
    def handler(request):
        if request.url.path.endswith("/claim"):
            return httpx.Response(200, json={"execution": grant.__dict__})
        if request.url.path.endswith("/heartbeat"):
            return httpx.Response(200, json={"lease_seconds": 60, "continue": False})
        return httpx.Response(200, json={"acknowledged": True})
    daemon = publication_daemon(repository, journal, tmp_path, handler=handler, checkpoint_seconds=0.01)
    daemon.harnesses["harness"] = HarnessConfig(Adapter(), tmp_path / "home", tmp_path / "scratch", unrestricted=True)
    assert daemon.run_once() == "succeeded"
    assert journal.recovery_health()[0]["succeeded_at"] is not None


def test_checkpoint_storage_failure_cancels_provider_and_keeps_work(repository, journal, tmp_path, monkeypatch):
    recovery = GitRecovery(repository, "repo-1", "execution-1", 1, journal)
    daemon = publication_daemon(repository, journal, tmp_path, checkpoint_seconds=0.01)
    (repository / "Proof.lean").write_text("must remain available\n")
    def full():
        raise JournalFull("protected storage exhausted")
    monkeypatch.setattr(recovery, "checkpoint_dirty", full)
    stopped, cancelled = threading.Event(), threading.Event()
    failures = []
    thread = threading.Thread(target=daemon._checkpoint_loop, args=(recovery, stopped, cancelled, failures))
    thread.start()
    try:
        assert cancelled.wait(5)
        assert isinstance(failures[0], JournalFull)
        assert (repository / "Proof.lean").read_text() == "must remain available\n"
    finally:
        stopped.set()
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_provider_boundary_waits_for_publishers_inflight_api_ack(repository, journal, tmp_path):
    sending, release, completed = threading.Event(), threading.Event(), threading.Event()
    def handler(request):
        sending.set()
        assert release.wait(5)
        return httpx.Response(200)
    daemon = publication_daemon(repository, journal, tmp_path, handler=handler)
    daemon._publisher_running = True
    item = operation()
    journal.enqueue(item)
    errors = []
    def flush(*, boundary=False):
        try:
            daemon._flush()
            if boundary:
                assert daemon._acknowledged(item)
                completed.set()
        except BaseException as error:
            errors.append(error)
    publisher = threading.Thread(target=flush)
    boundary = threading.Thread(target=flush, kwargs={"boundary": True})
    publisher.start()
    try:
        assert sending.wait(5)
        boundary.start()
        assert not completed.wait(0.05)
        release.set()
        assert completed.wait(5)
    finally:
        release.set()
        publisher.join(timeout=5)
        if boundary.ident is not None:
            boundary.join(timeout=5)
    assert not errors


@pytest.mark.parametrize("provider_exit,expected", [(0, "yielded"), (1, "failed")])
def test_checkpoint_timeout_retains_terminal_receipt_and_retries_after_restart(
        repository, journal, tmp_path, monkeypatch, provider_exit, expected):
    class Adapter:
        provider = "codex_exec"

        def command(self, **kwargs):
            return [sys.executable, "-c", "import pathlib,json,sys; "
                    "pathlib.Path('Proof.lean').write_text('valuable unpublished work'); "
                    "print(json.dumps({'type':'thread.started','thread_id':'retained-context'})); "
                    f"sys.exit({provider_exit})"]

    grant = ExecutionGrant("execution-1", "assignment-1", 1, 60, "harness", "workspace",
                           str(repository), "repo-1", "Continue")
    receipts = []

    def handler(request):
        if request.url.path.endswith("/claim"):
            return httpx.Response(200, json={"execution": grant.__dict__})
        if request.url.path.endswith("/heartbeat"):
            return httpx.Response(200, json={"lease_seconds": 60, "continue": False})
        receipts.append(json.loads(request.content))
        return httpx.Response(200, json={"acknowledged": True})

    daemon = publication_daemon(repository, journal, tmp_path, handler=handler)
    daemon.harnesses["harness"] = HarnessConfig(Adapter(), tmp_path / "home", tmp_path / "scratch", unrestricted=True)
    original = GitRecovery.checkpoint_dirty
    monkeypatch.setattr(GitRecovery, "checkpoint_dirty", lambda self: (_ for _ in ()).throw(
        subprocess.TimeoutExpired(["git", "add"], 300)))
    assert daemon.run_once() == expected
    row = journal.executions()[0]
    assert row["checkpoint"]["provider_thread_id"] == "retained-context"
    assert row["checkpoint"]["recovery_pending"] is True
    terminal = row["checkpoint"]["terminal_operation"]
    assert terminal["payload"]["status"] == expected
    assert "invalid_configuration" not in json.dumps(terminal)
    assert not any(item["kind"] == "execution_finished" for item in receipts)
    assert "TimeoutExpired" in journal.recovery_health()[0]["error"]
    assert daemon.recover() == []
    monkeypatch.setattr(GitRecovery, "checkpoint_dirty", original)
    row["checkpoint"]["recovery_retry_at"] = 0
    journal.checkpoint(row["execution_id"], row["epoch"], row["checkpoint"])
    restarted = publication_daemon(repository, journal, tmp_path, handler=handler)
    assert restarted.recover() == ["execution-1"]
    assert restarted.recover() == []
    restarted._flush()
    finished = [item for item in receipts if item["kind"] == "execution_finished"]
    assert len(finished) == 1 and finished[0]["operation_id"] == terminal["operation_id"]
    assert git(repository, "show", "refs/horizon/dirty/execution-1:Proof.lean") == "valuable unpublished work"


def test_progress_report_does_not_wait_for_journal_lock(repository, journal, tmp_path):
    daemon = publication_daemon(repository, journal, tmp_path)
    with journal._lock:
        thread = threading.Thread(target=lambda: daemon._progress("heartbeat", component="heartbeat:execution"))
        thread.start()
        thread.join(2)
        assert not thread.is_alive()
    progress = json.loads((journal.state_root / "worker-progress.json").read_text())
    assert progress["process_identity"] == process_identity(os.getpid())
    assert progress["boot_id"] == boot_identity()
    assert progress["components"]["heartbeat:execution"]["phase"] == "heartbeat"


def test_auxiliary_lane_progress_preserves_primary_watchdog(repository, journal, tmp_path):
    primary = publication_daemon(repository, journal, tmp_path)
    auxiliary_path = journal.state_root / "auxiliary-planner-progress.json"
    auxiliary = publication_daemon(repository, journal, tmp_path, progress_path=auxiliary_path)
    primary._progress("provider_running", component="primary")
    primary_path = journal.state_root / "worker-progress.json"
    original = primary_path.read_bytes()

    auxiliary._progress("claim", component="auxiliary")

    assert primary_path.read_bytes() == original
    progress = json.loads(auxiliary_path.read_text())
    assert set(progress["components"]) == {"auxiliary"}
    assert progress["components"]["auxiliary"]["phase"] == "claim"
    assert not auxiliary_path.with_suffix(".pending").exists()


def test_service_shutdown_yields_assignment_instead_of_cancelling_it(repository, journal, tmp_path):
    class Adapter:
        provider = "codex_exec"

        def command(self, **kwargs):
            raise AssertionError("a stopped daemon must not launch a provider")

    grant = ExecutionGrant("execution-1", "assignment-1", 1, 60, "harness", "workspace",
                           str(repository), "repo-1", "Continue")
    def handler(request):
        if request.url.path.endswith("/claim"):
            return httpx.Response(200, json={"execution": grant.__dict__})
        return httpx.Response(200, json={"acknowledged": True})
    daemon = publication_daemon(repository, journal, tmp_path, handler=handler)
    daemon.harnesses["harness"] = HarnessConfig(Adapter(), tmp_path / "home", tmp_path / "scratch", unrestricted=True)
    stopped = threading.Event()
    stopped.set()
    assert daemon.run_once(cancel=stopped) == "yielded"
    receipt = journal.executions()[0]["checkpoint"]["terminal_operation"]
    assert receipt["payload"]["reason"] == "daemon_stopping"


@pytest.mark.parametrize("separate_group", [False, True])
@pytest.mark.parametrize("cancelled", [False, True])
def test_provider_stops_descendants_before_workspace_release(repository, journal, tmp_path, separate_group, cancelled):
    journal.grant_lease("execution-1", 1, 30)
    command = [sys.executable, "-c", "import subprocess,sys,pathlib,time; "
               "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'], "
               f"stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,process_group={0 if separate_group else None}); "
               "pathlib.Path('child.pid').write_text(str(p.pid)); "
               f"time.sleep({30 if cancelled else 0})"]
    cancel = threading.Event()
    def interrupt():
        deadline = time.monotonic() + 5
        while not (repository / "child.pid").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        if cancelled:
            cancel.set()
    thread = threading.Thread(target=interrupt)
    thread.start()
    try:
        result = ProcessSupervisor(journal).run(command, prompt="", request_id="request-1",
            execution_id="execution-1", epoch=1, workspace=repository, env={"PATH": os.defpath}, cancel=cancel)
    finally:
        thread.join(6)
    assert result.status == ("cancelled" if cancelled else "succeeded")
    child = int((repository / "child.pid").read_text())
    stat = Path(f"/proc/{child}/stat")
    assert not stat.exists() or stat.read_text().rsplit(")", 1)[1].split()[0] in {"Z", "X"}


def test_supervisor_observes_native_completion_missing_from_stdout(repository, journal, tmp_path):
    from uuid import uuid4
    from archon_horizon.pipeline.worker.codex_lifecycle import CodexLifecycleObserver
    from test_pipeline_codex_lifecycle import CALL, output, record
    journal.grant_lease("execution-1", 1, 30)
    native, request_id = str(uuid4()), str(uuid4())
    source = tmp_path / "home" / ".codex" / "sessions" / "2026" / "09" / "30" / ("rollout-date-" + native + ".jsonl")
    source.parent.mkdir(parents=True)
    source.write_text(record({"id": native}, "session_meta"))
    observer = CodexLifecycleObserver(journal, tmp_path / "home", request_id=request_id,
        execution_id="execution-1", epoch=1, thread_record_id="thread-record", since=0)
    command = [sys.executable, "-c", "import json,pathlib,sys; "
        "print(json.dumps({'type':'thread.started','thread_id':sys.argv[1]}),flush=True); "
        "p=pathlib.Path(sys.argv[2]);p.write_text(p.read_text()+sys.argv[3])", native, str(source), record(CALL) + record(output())]
    result = ProcessSupervisor(journal, poll_seconds=0.01).run(command, prompt="Work", request_id=request_id,
        execution_id="execution-1", epoch=1, workspace=repository, env={"PATH": os.defpath}, lifecycle_observer=observer)
    assert result.status == "succeeded"
    observations = [value for value in journal.records()
                    if journal.operation(value["operation_id"]).kind == "provider_observed"]
    assert len(observations) == 1


def test_local_lane_failure_does_not_stop_the_daemon(repository, journal, tmp_path, monkeypatch):
    daemon = publication_daemon(repository, journal, tmp_path)
    stop = threading.Event()
    attempts = []

    def run_once(**kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise subprocess.TimeoutExpired("git add", 300)
        stop.set()

    monkeypatch.setattr(daemon, "run_once", run_once)
    daemon.serve(stop, slots=1, poll_seconds=0.01)
    assert len(attempts) == 2


@pytest.mark.parametrize("container", [False, True])
def test_storage_blocked_worker_reports_health_without_claiming(repository, journal, tmp_path, monkeypatch, container):
    stop = threading.Event()
    requests = []

    def handler(request):
        requests.append((request.url.path, json.loads(request.content)))
        stop.set()
        return httpx.Response(200, json={})

    daemon = publication_daemon(repository, journal, tmp_path, handler=handler)
    if container:
        from dataclasses import replace
        daemon.harnesses["container"] = replace(daemon.harnesses["harness"],
            sandbox=SandboxPolicy("image@sha256:" + "a" * 64))
    monkeypatch.setattr(journal, "diagnostic_capacity", lambda amount: False)
    daemon.serve(stop, slots=1, poll_seconds=0.01)
    assert len(requests) == 1
    assert requests[0][0] == "/api/v3/worker/hosts/host/heartbeat"
    assert requests[0][1]["health"]["status"] == "storage_pressure"
    assert requests[0][1]["health"]["capabilities"] == ({} if container else {"workspace_preparation": 1})
