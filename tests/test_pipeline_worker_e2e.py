"""Real localhost HTTP and subprocess coverage without contacting a model provider."""

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from uuid import UUID

from sqlalchemy import select, update
import uvicorn

from archon_horizon.pipeline.api import create_app
from archon_horizon.pipeline.auth import issue_credential
from archon_horizon.pipeline.records import get, transaction_lock
from archon_horizon.pipeline.schema import tables
from archon_horizon.pipeline.worker.daemon import HarnessConfig, WorkerDaemon
from archon_horizon.pipeline.worker.journal import DurableJournal
from archon_horizon.pipeline.worker.provider import HeadlessAdapter
from archon_horizon.pipeline.worker.transport import WorkerTransport
from test_pipeline_api import api_database
from test_pipeline_service import make_world


PROVIDER = r'''
import json, os, pathlib, subprocess, sys, urllib.request, uuid
if sys.argv[1:] == ["--version"]:
    print("codex-cli 1")
    sys.exit(0)
assert "HORIZON_HOST_TOKEN" not in os.environ
assert os.environ["HORIZON_PROVIDER_REQUEST_ID"]
assert os.environ["HORIZON_PROVIDER_THREAD_ID"]
skills = pathlib.Path(os.environ["HORIZON_SKILLS_DIR"])
assert (skills / "operations" / "horizon-pipeline" / "SKILL.md").is_file()
assert (skills / "subagents" / "reviewers" / "library-api.md").is_file()
assert (skills / "subagents" / "implementation" / "lean-worker.md").is_file()
assert "source-researcher" in (skills / "SUBAGENTS.md").read_text()
assert not skills.stat().st_mode & 0o222
home = pathlib.Path(os.environ["HOME"])
trace_file = home / "test-trace.json"
traces = json.loads(trace_file.read_text()) if trace_file.exists() else []
prompt = sys.stdin.read()
native = "native-test-context"
if traces:
    assert sys.argv[2:4] == ["resume", native], sys.argv
    assert "Amended goal after investigation" in prompt
    assert "# Available Skills" not in prompt
    assert "# Available Subagents" not in prompt
else:
    assert "resume" not in sys.argv
    assert "$HORIZON_SKILLS_DIR/operations/horizon-pipeline/SKILL.md" in prompt
    assert "# Available Skills" not in prompt
    assert "# Available Subagents" not in prompt
    index = (skills / "SKILLS.md").read_text()
    assert "lean/lean-performance/SKILL.md" in index
    assert "operations/horizon-zulip/SKILL.md" in index
    helpers = (skills / "SUBAGENTS.md").read_text()
    assert "subagents/implementation/lean-worker.md" in helpers
    assert "subagents/research/page-transcriber.md" in helpers
    assert "subagents/validation/orchestration-auditor.md" in helpers
print(json.dumps({"type":"thread.started","thread_id":native}), flush=True)
def api(method, path, value=None):
    body = json.dumps(value).encode() if value is not None else None
    request = urllib.request.Request(os.environ["HORIZON_API_URL"].rstrip("/") + "/api/v3" + path,
        data=body, method=method, headers={"Authorization":"Bearer " + os.environ["HORIZON_EXECUTION_TOKEN"],
        "Content-Type":"application/json", "Idempotency-Key":str(uuid.uuid4())})
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)
context = api("GET", "/assignments/" + os.environ["HORIZON_ASSIGNMENT_ID"] + "/context")
if not traces:
    mission = context["mission"]
    api("PATCH", "/missions/" + mission["id"], {"expected_revision":mission["revision"],
        "objective":"Amended goal after investigation"})
else:
    for item in context["obligations"]:
        if item["status"] == "open":
            api("POST", "/obligations/" + item["id"] + "/resolve", {"expected_revision":item["revision"], "status":"done",
                "resolution":{"kind":"completed","note":"Proof and amended goal handled"}})
pathlib.Path("Proof.lean").write_text("-- formalization work " + str(len(traces) + 1) + "\n")
subprocess.run(["git","add","Proof.lean"], check=True, stdout=subprocess.DEVNULL)
subprocess.run(["git","commit","-m","Formalization progress " + str(len(traces) + 1)], check=True, stdout=subprocess.DEVNULL)
traces.append({"request":os.environ["HORIZON_PROVIDER_REQUEST_ID"],"native":native,"args":sys.argv[1:],"prompt":prompt})
trace_file.write_text(json.dumps(traces))
print(json.dumps({"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":20,"cached_input_tokens":3}}), flush=True)
'''


def git(path, *args):
    return subprocess.run(["git", *args], cwd=path, text=True, capture_output=True, check=True).stdout.strip()


def test_real_http_worker_continues_amended_goal_and_preserves_commits(api_database, tmp_path):
    with api_database.transaction() as conn:
        transaction_lock(conn)
        world = make_world(conn, tmp_path / "control")
        run = world.run()
        world.disable_automations(run)
        assignment = world.assignment(run)
        _, host_token = issue_credential(conn, world.host_actor.id, "host_key", "Worker lifecycle test")
        workspaces = list(conn.execute(select(tables["workspace"]).where(
            tables["workspace"].c.project_id == world.project["id"])).mappings())
    for record in workspaces:
        root = Path(record["path"])
        root.mkdir(parents=True)
        git(root, "init")
        git(root, "config", "user.name", "Test worker")
        git(root, "config", "user.email", "test@example.invalid")
        (root / "Proof.lean").write_text("-- initial formalization\n")
        git(root, "add", ".")
        git(root, "commit", "-m", "Initial")
    remote = tmp_path / "forge.git"
    git(tmp_path, "init", "--bare", str(remote))
    provider = tmp_path / "fake-codex"
    provider.write_text("#!" + sys.executable + "\n" + PROVIDER)
    provider.chmod(0o700)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    config = world.service.config.model_copy(update={"public_url": url, "secure_cookies": False})
    server = uvicorn.Server(uvicorn.Config(create_app(config, database=api_database, background=False),
                                           log_level="error", access_log=False))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    journal = DurableJournal(tmp_path / "worker-journal")
    transport = WorkerTransport(url, host_token)
    home = tmp_path / "provider-home"
    harness = HarnessConfig(HeadlessAdapter("codex_exec", str(provider), model="test-model"), home,
                            tmp_path / "worker-scratch", unrestricted=True, provider_version="1", adapter_version="1")
    daemon = WorkerDaemon(host_id=str(world.host["id"]), journal=journal, transport=transport,
                          harnesses={str(world.harness["id"]): harness}, workspace_roots=(tmp_path / "control",),
                          publication_remotes={str(world.workspace_repo["id"]): str(remote)})
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        assert server.started
        outcome = daemon.run_once()
        assert outcome == "succeeded", [(row["state"], row["error"], row["envelope"]) for row in journal.records()]
        for _ in range(10):
            daemon._flush()
            if all(row["state"] == "acknowledged" for row in journal.records()):
                break
        assert all(row["state"] == "acknowledged" for row in journal.records())
        traces = json.loads((home / "test-trace.json").read_text())
        assert len(traces) == 2
        assert traces[0]["native"] == traces[1]["native"]
        assert traces[0]["request"] != traces[1]["request"]
        with api_database.transaction() as conn:
            assert get(conn, "assignment", assignment["id"])["status"] == "completed"
            native = conn.execute(select(tables["provider_thread"]).where(
                tables["provider_thread"].c.assignment_id == assignment["id"])).mappings().one()
            assert native["provider_thread_id"] == "native-test-context"
            requests = list(conn.execute(select(tables["provider_request"]).where(
                tables["provider_request"].c.provider_thread_id == native["id"])).mappings())
            assert len(requests) == 2 and all(row["status"] == "completed" for row in requests)
            publications = list(conn.execute(select(tables["publication"]).where(
                tables["publication"].c.requested_by_assignment_id == assignment["id"])).mappings())
            assert len(publications) >= 3 and all(row["status"] == "verified" for row in publications)
            for publication in publications:
                artifact = get(conn, "artifact", publication["artifact_id"])
                assert git(remote, "rev-parse", publication["target"]["ref_name"]) == artifact["content"]["commit_oid"]
            assert len(list(conn.execute(select(tables["usage_record"]).where(
                tables["usage_record"].c.provider_thread_id == native["id"])))) == 2
    finally:
        transport.close()
        journal.close()
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        assert not thread.is_alive()
