import hashlib
import json
from uuid import uuid4

import httpx
import pytest

from archon_horizon.pipeline.reference_cache import ReferenceCache, ReferenceCacheError


def response(content=b"@article{entry, title={A theorem}}", revision=1, status=200):
    return httpx.Response(status, content=content if status == 200 else b"", headers={
        "ETag": f'"reference-{revision}"', "X-Horizon-Reference-Revision": str(revision),
        "Content-Type": "application/x-bibtex"})


def test_authenticated_revalidation_uses_etag_and_verified_workspace_content(tmp_path):
    seen = []
    def serve(request):
        seen.append(request)
        assert request.headers["authorization"] == "Bearer private-token"
        return response(status=304) if request.headers.get("if-none-match") else response()
    identifier = uuid4()
    with ReferenceCache(tmp_path, "https://horizon.invalid", "private-token", client=httpx.Client(transport=httpx.MockTransport(serve))) as cache:
        first, second = cache.get(identifier), cache.get(identifier)
    assert first == second
    assert first.path.is_relative_to(tmp_path / ".horizon" / "reference-cache")
    assert first.path.read_bytes() == first.content
    assert first.sha256 == hashlib.sha256(first.content).hexdigest()
    assert seen[1].headers["if-none-match"] == '"reference-1"'
    assert "private-token" not in first.path.with_suffix(".json").read_text()


def test_corruption_refetches_unconditionally_and_new_revision_replaces_entry(tmp_path):
    calls = []
    def serve(request):
        calls.append(request)
        return response(revision=len(calls))
    cache = ReferenceCache(tmp_path, "https://horizon.invalid", "token", client=httpx.Client(transport=httpx.MockTransport(serve)))
    first = cache.get(uuid4())
    first.path.write_bytes(b"corrupted")
    second = cache.get(first.reference_id)
    assert "if-none-match" not in calls[1].headers
    assert second.revision == 2 and second.path.read_bytes() == second.content
    third = cache.get(first.reference_id)
    assert third.revision == 3
    assert json.loads(third.path.with_suffix(".json").read_text())["revision"] == 3


@pytest.mark.parametrize("status", [401, 403, 404, 410])
def test_revoked_or_deleted_reference_never_falls_back_to_cached_export(tmp_path, status):
    replies = iter([response(), response(status=status)])
    cache = ReferenceCache(tmp_path, "https://horizon.invalid", "token", client=httpx.Client(transport=httpx.MockTransport(lambda _: next(replies))))
    first = cache.get(uuid4())
    with pytest.raises(httpx.HTTPStatusError):
        cache.get(first.reference_id)
    assert not first.path.exists()


def test_offline_cache_read_does_not_bypass_authorization(tmp_path):
    calls = 0
    def serve(request):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise httpx.ConnectError("offline", request=request)
        return response()
    cache = ReferenceCache(tmp_path, "https://horizon.invalid", "token", client=httpx.Client(transport=httpx.MockTransport(serve)))
    first = cache.get(uuid4())
    with pytest.raises(httpx.ConnectError):
        cache.get(first.reference_id)
    assert first.path.exists()


def test_lru_eviction_and_stream_byte_limit_are_bounded(tmp_path):
    cache = ReferenceCache(tmp_path, "https://horizon.invalid", "token", max_entries=2,
        client=httpx.Client(transport=httpx.MockTransport(lambda _: response())))
    first, second = cache.get(uuid4()), cache.get(uuid4())
    cache.get(first.reference_id)
    third = cache.get(uuid4())
    assert first.path.exists() and third.path.exists() and not second.path.exists()
    assert len(list(cache.root.glob("*.bib"))) == 2
    large = ReferenceCache(tmp_path, "https://horizon.invalid", "token", max_entry_bytes=8,
        client=httpx.Client(transport=httpx.MockTransport(lambda _: response(b"x" * 40))))
    with pytest.raises(ReferenceCacheError, match="exceeds"):
        large.get(uuid4())


def test_cache_partition_and_redirects_do_not_leak_credentials(tmp_path):
    identifier = uuid4()
    cache = ReferenceCache(tmp_path, "https://horizon.invalid", "first", client=httpx.Client(transport=httpx.MockTransport(lambda _: response())))
    first = cache.get(identifier)
    seen = []
    def redirect(request):
        seen.append(request)
        assert "if-none-match" not in request.headers
        return httpx.Response(302, headers={"Location": "https://attacker.invalid/file"})
    other = ReferenceCache(tmp_path, "https://horizon.invalid", "second", client=httpx.Client(transport=httpx.MockTransport(redirect)))
    with pytest.raises(httpx.HTTPStatusError):
        other.get(identifier)
    assert len(seen) == 1 and seen[0].url.host == "horizon.invalid"
    assert first.path.exists()


def test_inconsistent_304_retries_once_without_validator(tmp_path):
    replies = iter([response(), response(revision=2, status=304), response(revision=2)])
    seen = []
    def serve(request):
        seen.append(request)
        return next(replies)
    cache = ReferenceCache(tmp_path, "https://horizon.invalid", "token", client=httpx.Client(transport=httpx.MockTransport(serve)))
    first = cache.get(uuid4())
    assert cache.get(first.reference_id).revision == 2
    assert "if-none-match" in seen[1].headers and "if-none-match" not in seen[2].headers


def test_cache_rejects_workspace_escape_symlink(tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (tmp_path / ".horizon").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ReferenceCacheError, match="symlink"):
        ReferenceCache(tmp_path, "https://horizon.invalid", "token")
    assert not list(outside.iterdir())
