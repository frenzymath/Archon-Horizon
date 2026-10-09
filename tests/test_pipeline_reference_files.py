"""Source-document transfers exercise real permissions, storage, and recovery."""
import hashlib
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete, insert, update

from archon_horizon.pipeline.auth import issue_credential
from archon_horizon.pipeline.client import AgentClient, RetryDeferred
from archon_horizon.pipeline.persistence.records import create
from archon_horizon.pipeline.persistence.schema import tables
from archon_horizon.pipeline.projects.reference_files_client import download_file, upload_file
from test_pipeline_api import api, api_database, auth, mutate


def reference(api):
    client, _, world, _, token, _ = api
    response = mutate(client, "/api/v3/records/reference", {"project_id": str(world.project["id"]),
        "cite_key": "source-paper", "kind": "article", "title": "A source paper", "authors": ["A. Author"], "issued_year": 2026}, token)
    assert response.status_code == 200, response.text
    return response.json()


def upload(api, ref, content=b"%PDF-1.7\nsource\n", filename="paper.pdf", key=None, **params):
    client, _, _, _, token, _ = api
    return client.post(f"/api/v3/references/{ref['id']}/files", params={"filename": filename, **params}, content=content,
        headers={**auth(token), "Idempotency-Key": key or str(uuid4()), "Content-Type": "application/octet-stream"})


def test_large_document_dedup_versions_preview_and_archive(api):
    client, _, _, _, token, _ = api
    ref = reference(api)
    content = b"%PDF-1.7\n" + b"x" * (2 * 1024**2)  # Exceeds the ordinary JSON limit.
    key = str(uuid4())
    first = upload(api, ref, content, key=key, source_url="https://arxiv.org/pdf/2601.00001v1")
    assert first.status_code == 200, first.text
    file = first.json()
    assert file["sha256"] == hashlib.sha256(content).hexdigest()
    assert upload(api, ref, content, key=key, source_url="https://arxiv.org/pdf/2601.00001v1").json() == file
    assert upload(api, ref, content, source_url="https://arxiv.org/pdf/2601.00001v1").json()["id"] == file["id"]
    assert upload(api, ref, content + b"changed", key=key).status_code == 409
    next_version = upload(api, ref, b"%PDF-1.7\nsecond version").json()
    assert next_version["id"] != file["id"]
    url = file["content_url"]
    downloaded = client.get(url, headers=auth(token))
    assert downloaded.content == content
    assert downloaded.headers["content-disposition"].startswith("attachment;")
    preview = client.get(url, params={"preview": "true"}, headers=auth(token))
    assert preview.content == content and preview.headers["content-type"] == "application/pdf"
    assert preview.headers["content-disposition"].startswith("inline;")
    assert client.get(url, headers={**auth(token), "If-None-Match": downloaded.headers["etag"]}).status_code == 304
    listing = client.get(f"/api/v3/references/{ref['id']}/files", headers=auth(token)).json()
    assert len(listing["items"]) == 2 and listing["max_upload_bytes"] == 64 * 1024**2
    archived = mutate(client, f"/api/v3/references/{ref['id']}/files/{file['id']}/archive", {}, token)
    assert archived.status_code == 200, archived.text
    assert client.get(url, headers=auth(token)).status_code == 410
    assert len(client.get(f"/api/v3/references/{ref['id']}/files", headers=auth(token)).json()["items"]) == 1


@pytest.mark.parametrize("filename,content", [("../paper.pdf", b"%PDF-1"), ("bad\nname.pdf", b"%PDF-1"),
    ("paper.pdf", b"<html>"), ("empty.txt", b"")])
def test_invalid_names_or_contents_leave_no_reference_file(api, filename, content):
    ref = reference(api)
    assert upload(api, ref, content, filename).status_code == 422
    client, _, _, _, token, _ = api
    assert client.get(f"/api/v3/references/{ref['id']}/files", headers=auth(token)).json()["items"] == []


def test_text_and_archives_never_execute_on_dashboard_origin(api):
    client, _, _, _, token, _ = api
    ref = reference(api)
    content = b"\\section{Result}\n<script>alert('not executable')</script>"
    text = upload(api, ref, content, "source.tex").json()
    response = client.get(text["content_url"], params={"preview": True}, headers=auth(token))
    assert response.content == content and response.headers["content-type"].startswith("text/plain")
    assert response.headers["x-content-type-options"] == "nosniff"
    html = upload(api, ref, b"<script>alert(1)</script>", "companion.html").json()
    assert html["previewable"] is False
    assert client.get(html["content_url"], params={"preview": True}, headers=auth(token)).status_code == 422
    assert client.get(html["content_url"], headers=auth(token)).headers["content-type"] == "application/octet-stream"
    legacy = upload(api, ref, b"TeX in a legacy encoding: \xff", "legacy.tex").json()
    assert legacy["previewable"] is False
    assert client.get(legacy["content_url"], headers=auth(token)).content == b"TeX in a legacy encoding: \xff"


def test_authorization_is_rechecked_for_conditional_reads_and_file_scope(api):
    client, database, world, _, token, _ = api
    ref = reference(api)
    file = upload(api, ref).json()
    with database.transaction() as conn:
        principal = create(conn, "principal", kind="human", username="file-reader-" + uuid4().hex, display_name="Reader")
        conn.execute(insert(tables["project_grant"]).values(principal_id=principal["id"], project_id=world.project["id"], role="viewer"))
        _, reader_token = issue_credential(conn, principal["id"], "api_key", "Files")
        other = create(conn, "reference", project_id=world.project["id"], cite_key="other", kind="article", title="Other")
    url = file["content_url"]
    assert client.get(url).status_code == 401
    response = client.get(url, headers=auth(reader_token)); assert response.status_code == 200
    assert client.post(f"/api/v3/references/{ref['id']}/files", params={"filename": "paper.pdf"}, content=b"%PDF-1",
        headers={**auth(reader_token), "Idempotency-Key": str(uuid4())}).status_code == 403
    wrong = f"/api/v3/references/{other['id']}/files/{file['id']}/content"
    assert client.get(wrong, headers=auth(token)).status_code == 404
    with database.transaction() as conn:
        conn.execute(delete(tables["project_grant"]).where(tables["project_grant"].c.principal_id == principal["id"]))
    assert client.get(url, headers={**auth(reader_token), "If-None-Match": response.headers["etag"]}).status_code == 403


def test_stream_bound_and_failed_upload_clean_scratch(api):
    client, _, _, _, token, _ = api
    ref = reference(api)
    client.app.state.service.config.max_reference_file_bytes = 8
    route = f"/api/v3/references/{ref['id']}/files"
    response = client.post(route, params={"filename": "too-big.txt"}, content=iter([b"12345", b"67890"]),
        headers={**auth(token), "Idempotency-Key": str(uuid4())})
    assert response.status_code == 413, response.text
    assert not list(client.app.state.service.config.scratch_root.glob("reference-*"))
    assert client.post(route, params={"filename": "a.txt"}, content=b"x", headers={**auth(token),
        "Idempotency-Key": str(uuid4()), "Origin": "https://another.invalid"}).status_code == 403


def test_binary_journal_replays_snapshot_after_source_changes(tmp_path):
    reference_id = uuid4(); source = tmp_path / "paper.tex"; original = b"\\section{Original}"; source.write_bytes(original)
    seen = []; failing = True
    def serve(request):
        seen.append((request.read(), request.headers["idempotency-key"]))
        if failing: return httpx.Response(503, json={"error": {"code": "backend_unavailable"}})
        return httpx.Response(200, json={"id": "stored"})
    with httpx.Client(transport=httpx.MockTransport(serve)) as transport:
        client = AgentClient("https://horizon.invalid", "credential", "execution-1", tmp_path / "journal", client=transport)
        with pytest.raises(RetryDeferred):upload_file(client, reference_id, source)
        assert client.pending() and original not in client.path.read_bytes()
        source.write_bytes(b"edited after the failed acknowledgement")
        failing = False; client.execution_id = "execution-2"
        result = client.replay_pending()
        assert result["completed"] and result["pending"] == 0
        assert seen == [(original, seen[0][1]), (original, seen[0][1])]


def test_download_rejects_corruption_redirects_and_existing_destinations(tmp_path):
    content = b"source"; digest = hashlib.sha256(content).hexdigest()
    replies = iter([httpx.Response(200, content=b"corrupt", headers={"X-Content-SHA256": digest}),
        httpx.Response(302, headers={"Location": "https://untrusted.invalid/file"}),
        httpx.Response(200, content=content, headers={"X-Content-SHA256": digest})])
    seen = []
    def serve(request):seen.append(request); return next(replies)
    with httpx.Client(transport=httpx.MockTransport(serve)) as transport:
        client = AgentClient("https://horizon.invalid", "credential", "execution", tmp_path / "journal", client=transport)
        target=tmp_path / "paper.tex"
        with pytest.raises(ValueError, match="checksum"):download_file(client, uuid4(), uuid4(), target)
        assert not target.exists()
        with pytest.raises(httpx.HTTPStatusError):download_file(client, uuid4(), uuid4(), target)
        assert len(seen) == 2 and all(request.url.host == "horizon.invalid" for request in seen)
        result=download_file(client, uuid4(), uuid4(), target)
        assert target.read_bytes() == content and result["sha256"] == digest
        with pytest.raises(ValueError, match="unused"):download_file(client, uuid4(), uuid4(), target)
