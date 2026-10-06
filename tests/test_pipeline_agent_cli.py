import base64
import json
import sqlite3
import stat
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest

from archon_horizon.pipeline.cli import agent_command, parser
from test_pipeline_api import api, api_database


@pytest.mark.parametrize("options,suffix", [([], ""), (["--view", "full"], "?view=full"),
                                          (["--view", "operations"], "?view=operations")])
def test_agent_context_selects_the_existing_scoped_view(monkeypatch, tmp_path, capsys, options, suffix):
    from archon_horizon.pipeline import client
    calls = []

    class TestClient:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, method, path):
            calls.append((method, path))
            return {"view": "test"}

        def close(self):
            calls.append("closed")

    for key, value in {
        "HORIZON_API_URL": "https://test.invalid", "HORIZON_EXECUTION_TOKEN": "test-only-token",
        "HORIZON_EXECUTION_ID": "test-execution", "HORIZON_ASSIGNMENT_ID": "test-assignment",
        "HORIZON_AGENT_STATE": str(tmp_path),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(client, "AgentClient", TestClient)
    agent_command(parser().parse_args(["agent", "context", *options]))
    assert calls == [("GET", "/api/v3/assignments/test-assignment/context" + suffix), "closed"]
    assert json.loads(capsys.readouterr().out) == {"view": "test"}


@pytest.mark.parametrize("options,query", [
    ([], {}),
    (["--section", "routes"], {"section": ["routes"]}),
    (["--section", "queries", "--name", "GET /api/v3/forge-items/{identifier}/inspect"],
     {"section": ["queries"], "name": ["GET /api/v3/forge-items/{identifier}/inspect"]}),
])
def test_agent_schema_requests_the_selected_contract(monkeypatch, tmp_path, capsys, options, query):
    from archon_horizon.pipeline import client

    calls = []

    class TestClient:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, method, path):
            calls.append((method, path))
            return {"contract": "test fixture"}

        def close(self):
            calls.append("closed")

    for key, value in {
        "HORIZON_API_URL": "https://test.invalid", "HORIZON_EXECUTION_TOKEN": "test-only-token",
        "HORIZON_EXECUTION_ID": "test-execution", "HORIZON_ASSIGNMENT_ID": "test-assignment",
        "HORIZON_AGENT_STATE": str(tmp_path),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(client, "AgentClient", TestClient)
    agent_command(parser().parse_args(["agent", "schema", *options]))
    assert calls[0][0] == "GET"
    url = urlsplit(calls[0][1])
    assert url.path == "/api/v3/schema"
    assert parse_qs(url.query) == query
    assert calls[-1] == "closed"
    assert json.loads(capsys.readouterr().out) == {"contract": "test fixture"}


@pytest.mark.parametrize("section,name,operation", [
    ("command_args", "prepare_reviewer", "prepare_reviewer"),
    ("create", "POST /api/v3/forge/change", "forge_change"),
])
def test_agent_schema_rejection_exposes_server_correction_without_alias_retry(api, monkeypatch, tmp_path, section, name, operation):
    import httpx
    from archon_horizon.pipeline import client

    server, _, _, _, token, _ = api
    requests = []

    def respond(request):
        requests.append(request)
        result = server.get(request.url.raw_path.decode(), headers={"Authorization": request.headers["Authorization"]})
        return httpx.Response(result.status_code, content=result.content)

    actual_client = client.AgentClient
    with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
        monkeypatch.setattr(client, "AgentClient", lambda *args, **kwargs: actual_client(*args, client=transport, **kwargs))
        for key, value in {
            "HORIZON_API_URL": "https://test.invalid", "HORIZON_EXECUTION_TOKEN": token,
            "HORIZON_EXECUTION_ID": "test-execution", "HORIZON_ASSIGNMENT_ID": "test-assignment",
            "HORIZON_AGENT_STATE": str(tmp_path / "journal"),
        }.items():
            monkeypatch.setenv(key, value)
        with pytest.raises(RuntimeError) as rejected:
            agent_command(parser().parse_args(["agent", "schema", "--section", section, "--name", name]))
    assert f"horizon-pipeline agent schema --section operations --name {operation}" in str(rejected.value)
    assert "Horizon 422" in str(rejected.value)
    assert len(requests) == 1


def test_agent_query_schema_accepts_a_route_copied_with_concrete_kind(api, monkeypatch, tmp_path, capsys):
    import httpx
    from archon_horizon.pipeline import client

    server, _, _, _, token, _ = api
    requests = []
    def respond(request):
        requests.append(request)
        response = server.get(request.url.raw_path.decode(), headers={"Authorization": request.headers["Authorization"]})
        return httpx.Response(response.status_code, content=response.content)
    actual_client = client.AgentClient
    with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
        monkeypatch.setattr(client, "AgentClient", lambda *args, **kwargs: actual_client(*args, client=transport, **kwargs))
        for key, value in {"HORIZON_API_URL": "https://test.invalid", "HORIZON_EXECUTION_TOKEN": token,
                           "HORIZON_EXECUTION_ID": "test-execution", "HORIZON_ASSIGNMENT_ID": "test-assignment",
                           "HORIZON_AGENT_STATE": str(tmp_path / "journal")}.items():
            monkeypatch.setenv(key, value)
        agent_command(parser().parse_args(["agent", "schema", "--section", "queries", "--name",
                                          "GET /api/v3/records/document/{id}"]))
    output = json.loads(capsys.readouterr().out)
    assert {parameter["name"] for parameter in output["parameters"]} >= {"kind", "identifier"}
    assert len(requests) == 1


@pytest.fixture
def reviewer_cli(monkeypatch, tmp_path):
    import httpx
    from archon_horizon.pipeline import client

    requests = []
    bundle = {"execution_id": "test-execution", "accounts": [{"slug": "definitions", "token": "reviewer-private-token"}]}

    def respond(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer execution-private-token"
        assert request.url.path == "/api/v3/executions/test-execution/reviewer-accounts"
        return httpx.Response(200, json=bundle)

    actual_client = client.AgentClient
    with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
        monkeypatch.setattr(client, "AgentClient", lambda *args, **kwargs: actual_client(*args, client=transport, **kwargs))
        for key, value in {
            "HORIZON_API_URL": "https://test.invalid", "HORIZON_EXECUTION_TOKEN": "execution-private-token",
            "HORIZON_EXECUTION_ID": "test-execution", "HORIZON_ASSIGNMENT_ID": "test-assignment",
            "HORIZON_AGENT_STATE": str(tmp_path / "journal"), "TMPDIR": str(tmp_path / "scratch"),
        }.items():
            monkeypatch.setenv(key, value)
        monkeypatch.delenv("HORIZON_REVIEWER_ACCOUNTS_FILE", raising=False)
        yield requests, bundle


@pytest.mark.parametrize("existing_path", [False, True])
def test_reviewer_accounts_print_only_private_file_path(monkeypatch, tmp_path, capsys, reviewer_cli, existing_path):
    requests, bundle = reviewer_cli
    target = tmp_path / "scratch" / "reviewer-accounts.json"
    if existing_path:
        target = tmp_path / "custom-reviewers.json"
        target.write_text("old credentials")
        target.chmod(0o644)
        monkeypatch.setenv("HORIZON_REVIEWER_ACCOUNTS_FILE", str(target))
    agent_command(parser().parse_args(["agent", "reviewer-accounts"]))
    output = capsys.readouterr()
    assert output.out == str(target) + "\n" and output.err == ""
    assert json.loads(target.read_text()) == bundle
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert len(requests) == 1
    assert b"reviewer-private-token" not in (tmp_path / "journal" / "api-intents.sqlite3").read_bytes()
    assert not list(target.parent.glob(".reviewer-accounts-*"))


@pytest.mark.parametrize("suffix", ["reviewer-accounts", "reviewer-accounts/", "%72eviewer-accounts?extra=1"])
def test_raw_reviewer_account_request_refuses_secret_output(capsys, reviewer_cli, suffix):
    requests, _bundle = reviewer_cli
    with pytest.raises(ValueError, match="agent reviewer-accounts"):
        agent_command(parser().parse_args(["agent", "request", "GET", f"/api/v3/executions/test-execution/{suffix}"]))
    assert requests == []
    assert capsys.readouterr().out == ""


def test_reviewer_accounts_refuse_symlink_output(monkeypatch, tmp_path, reviewer_cli):
    requests, _bundle = reviewer_cli
    original = tmp_path / "original"
    original.write_text("preserve")
    link = tmp_path / "link"
    link.symlink_to(original)
    monkeypatch.setenv("HORIZON_REVIEWER_ACCOUNTS_FILE", str(link))
    with pytest.raises(ValueError, match="non-symlink"):
        agent_command(parser().parse_args(["agent", "reviewer-accounts"]))
    assert original.read_text() == "preserve"
    assert requests == []


@pytest.fixture
def control_cli(monkeypatch, tmp_path):
    import httpx
    from archon_horizon.pipeline import client

    now = [1000.0]
    notice = {"id": "notice-id", "revision": 1, "excerpt": "Use the completed exact-head check.",
              "truncated": False, "detail_url": "/api/v3/notifications/notice-id"}
    state = {"calls": [], "clock": now, "summary": {"pending": 1, "omitted": 0, "notices": [notice]},
             "notice_status": 200, "operation_status": 200}
    def respond(request):
        state["calls"].append(request)
        if request.url.path.endswith("/control-notices"):
            assert "Idempotency-Key" not in request.headers
            if state.get("notice_failure"):
                raise httpx.ConnectError("notice read unavailable", request=request)
            return httpx.Response(state["notice_status"], json=state["summary"])
        return httpx.Response(state["operation_status"], json={"result": "unchanged"})
    actual_client = client.AgentClient
    with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
        monkeypatch.setattr(client, "AgentClient", lambda *args, **kwargs: actual_client(*args, client=transport, **kwargs))
        monkeypatch.setattr(client.time, "time", lambda: now[0])
        for key, value in {"HORIZON_API_URL": "https://test.invalid", "HORIZON_EXECUTION_TOKEN": "test-only-token",
                           "HORIZON_EXECUTION_ID": "test-execution", "HORIZON_ASSIGNMENT_ID": "test-assignment",
                           "HORIZON_AGENT_STATE": str(tmp_path / "journal"), "CODEX_THREAD_ID": "parent-thread"}.items():
            monkeypatch.setenv(key, value)
        yield state


def test_agent_request_surfaces_notices_on_stderr_without_changing_json_or_acknowledging(control_cli, capsys):
    args = parser().parse_args(["agent", "request", "POST", "/api/v3/records/obligation", '{"description":"test"}'])
    agent_command(args)
    output = capsys.readouterr()
    assert json.loads(output.out) == {"result": "unchanged"}
    assert "Use the completed exact-head check" in output.err
    assert "does not acknowledge it" in output.err
    assert [(r.method, r.url.path) for r in control_cli["calls"]] == [
        ("POST", "/api/v3/records/obligation"), ("GET", "/api/v3/assignments/test-assignment/control-notices")]
    assert control_cli["calls"][0].headers["Idempotency-Key"]
    assert control_cli["calls"][1].extensions["timeout"]["read"] == 2


def test_agent_request_body_file_preserves_text_and_journal_identity(control_cli, tmp_path):
    payload = {"instructions": "Review the author's contract.\nLiteral `code`, $(text), and \\\"quotes\\\"."}
    path = tmp_path / "review.json"
    path.write_text(json.dumps(payload))
    agent_command(parser().parse_args(["agent", "request", "POST", "/api/v3/reviewer-assignments",
                                      "--body-file", str(path), "--key", "file-request"]))
    request = control_cli["calls"][0]
    assert json.loads(request.content) == payload
    assert request.headers["Idempotency-Key"] == "file-request"
    with sqlite3.connect(tmp_path / "journal" / "api-intents.sqlite3") as journal:
        assert journal.execute("SELECT count(*) FROM intent").fetchone()[0] == 1


@pytest.mark.parametrize("content", [b"not json", b" " * (1024**2 + 1)])
def test_agent_request_invalid_body_file_sends_no_request(control_cli, tmp_path, content):
    path = tmp_path / "invalid.json"
    path.write_bytes(content)
    with pytest.raises(ValueError):
        agent_command(parser().parse_args(["agent", "request", "POST", "/api/v3/reviewer-assignments",
                                          "--body-file", str(path)]))
    assert control_cli["calls"] == []


def test_agent_request_rejects_two_body_sources(tmp_path):
    with pytest.raises(SystemExit):
        parser().parse_args(["agent", "request", "POST", "/api/v3/reviewer-assignments", "{}",
                             "--body-file", str(tmp_path / "body.json")])


def test_notice_throttle_deduplication_revision_and_context_consumers(control_cli, capsys, monkeypatch):
    args = parser().parse_args(["agent", "request", "GET", "/api/v3/records/assignment/test-assignment"])
    agent_command(args)
    assert "notice-id" in capsys.readouterr().err
    agent_command(args)
    assert capsys.readouterr().err == ""
    assert len(control_cli["calls"]) == 3
    control_cli["clock"][0] += 31
    agent_command(args)
    assert capsys.readouterr().err == ""
    assert len(control_cli["calls"]) == 5
    control_cli["summary"]["notices"][0]["revision"] = 2
    control_cli["clock"][0] += 31
    agent_command(args)
    assert '"revision": 2' in capsys.readouterr().err
    monkeypatch.setenv("CODEX_THREAD_ID", "child-thread")
    agent_command(args)
    assert "notice-id" in capsys.readouterr().err
    monkeypatch.setenv("CODEX_THREAD_ID", "parent-thread")
    control_cli["clock"][0] += 301
    agent_command(args)
    assert "notice-id" in capsys.readouterr().err


@pytest.mark.parametrize("failure", ["transport", "http", "oversized", "malformed"])
@pytest.mark.parametrize("operation_status", [200, 409])
def test_notice_fetch_failure_preserves_original_request_outcome(control_cli, capsys, failure, operation_status):
    control_cli["operation_status"] = operation_status
    if failure == "transport":
        control_cli["notice_failure"] = True
    elif failure == "http":
        control_cli["notice_status"] = 503
    elif failure == "oversized":
        control_cli["summary"]["notices"][0]["excerpt"] = "x" * 17000
    else:
        control_cli["summary"] = ["invalid summary"]
    args = parser().parse_args(["agent", "request", "POST", "/api/v3/commands", '{}'])
    if operation_status == 200:
        agent_command(args)
        assert json.loads(capsys.readouterr().out) == {"result": "unchanged"}
    else:
        with pytest.raises(RuntimeError, match="Horizon 409"):
            agent_command(args)
    assert capsys.readouterr().err == ""
    assert len(control_cli["calls"]) == 2


@pytest.mark.parametrize("arguments", [["context"], ["request", "GET", "/api/v3/assignments/test-assignment/context?view=full"],
                                      ["request", "GET", "/api/v3/assignments/test-assignment/control-notices"]])
def test_explicit_notice_and_context_reads_do_not_fetch_redundant_summary(control_cli, capsys, arguments):
    agent_command(parser().parse_args(["agent", *arguments]))
    assert len(control_cli["calls"]) == 1
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("media_type", [None, "application/octet-stream"])
def test_upload_file_preserves_exact_bytes_and_uses_journal(control_cli, tmp_path, capsys, media_type):
    project_id = str(uuid4())
    content = b"source with CRLF\r\n\x00\xff\xc3\xa9\n"
    source = tmp_path / "proposed source.lean"
    source.write_bytes(content)
    arguments = ["agent", "upload-file", str(source), "--project-id", project_id]
    if media_type:
        arguments.extend(["--media-type", media_type])

    agent_command(parser().parse_args(arguments))

    assert json.loads(capsys.readouterr().out) == {"result": "unchanged"}
    assert len(control_cli["calls"]) == 1
    request = control_cli["calls"][0]
    assert (request.method, request.url.path) == ("POST", "/api/v3/artifacts")
    payload = json.loads(request.content)
    assert payload["project_id"] == project_id
    assert payload["media_type"] == (media_type or "text/plain")
    assert base64.b64decode(payload["content_base64"], validate=True) == content
    with sqlite3.connect(tmp_path / "journal" / "api-intents.sqlite3") as journal:
        key, status, body = journal.execute("SELECT id,status,body FROM intent").fetchone()
    assert key == request.headers["Idempotency-Key"]
    assert status == "completed" and json.loads(body) == payload


def test_upload_file_rejects_oversize_before_request(control_cli, tmp_path, capsys):
    source = tmp_path / "large-source"
    with source.open("wb") as stream:
        stream.truncate(1024**2)
    arguments = ["agent", "upload-file", str(source), "--project-id", str(uuid4())]

    with pytest.raises(ValueError, match="exceeds the journaled upload limit"):
        agent_command(parser().parse_args(arguments))

    assert control_cli["calls"] == []
    assert capsys.readouterr().out == ""
    with sqlite3.connect(tmp_path / "journal" / "api-intents.sqlite3") as journal:
        assert journal.execute("SELECT count(*) FROM intent").fetchone()[0] == 0


@pytest.mark.parametrize("failure", ["directory", "invalid_media_type"])
def test_upload_file_rejects_invalid_input_without_request(control_cli, tmp_path, failure):
    source = tmp_path if failure == "directory" else tmp_path / "source"
    if failure != "directory":
        source.write_bytes(b"source")
    arguments = ["agent", "upload-file", str(source), "--project-id", str(uuid4())]
    if failure == "invalid_media_type":
        arguments.extend(["--media-type", "text/plain; charset=utf-8"])
    with pytest.raises(ValueError):
        agent_command(parser().parse_args(arguments))
    assert control_cli["calls"] == []
