import json
from uuid import uuid4

import httpx
from pydantic import ValidationError
import pytest

from archon_horizon.pipeline.connectors import ForgejoClient
from archon_horizon.pipeline.models import ForgeReview


def review(**updates):
    return ForgeReview.model_validate({"forge_item_id": uuid4(), "commit_oid": "a" * 40,
        "verdict": "changes_requested", "summary": "The definition loses an endpoint.", **updates})


@pytest.mark.parametrize("comment", [
    {"path": "../secret", "body": "Finding", "new_position": 1},
    {"path": "Proof.lean", "body": "Finding"},
    {"path": "Proof.lean", "body": "Finding", "new_position": 1, "old_position": 1},
    {"path": "Proof.lean", "body": "Finding", "new_position": True},
])
def test_inline_review_comments_require_a_repository_path_and_unambiguous_line(comment):
    with pytest.raises(ValidationError):
        review(comments=[comment])


def test_inline_findings_share_one_review_and_reconcile_lost_response():
    created = []
    data = review(comments=[{"path": "Geometry/Curve.lean", "body": "This excludes closed curves.", "new_position": 18}])
    def handler(request):
        if request.url.path.endswith("/user"):
            return httpx.Response(200, json={"id": 7})
        if request.url.path.endswith("/reviews"):
            if request.method == "GET":
                return httpx.Response(200, json=created)
            payload = json.loads(request.content)
            assert payload["comments"] == [data.comments[0].model_dump()]
            assert payload["event"] == "REQUEST_CHANGES"
            assert payload["commit_id"] == "a" * 40
            created.append({"id": 11, "user": {"id": 7}, **payload})
            raise httpx.ReadTimeout("lost review receipt")
        return httpx.Response(200, json={"head": {"sha": "a" * 40}, "state": "open"})
    client = ForgejoClient("https://forge.test", "test-only", client=httpx.Client(transport=httpx.MockTransport(handler)))
    from archon_horizon.pipeline.connectors import ConnectorFailure
    args = {"expected_head": data.commit_oid, "body": data.summary, "verdict": data.verdict,
            "operation_id": "inline-review", "comments": [c.model_dump() for c in data.comments]}
    with pytest.raises(ConnectorFailure) as error:
        client.submit_review("owner/repo", 1, **args)
    assert error.value.uncertain
    result = client.submit_review("owner/repo", 1, reconcile_only=True, **args)
    assert result["id"] == 11 and len(created) == 1


@pytest.mark.parametrize("updates", [
    {"verdict": "approved"}, {"provider_request_id": None},
    {"comments": [{"path": "Proof.lean", "body": "Old line", "new_position": 1}]},
])
def test_historical_feedback_cannot_approve_or_attach_stale_lines(updates):
    with pytest.raises(ValidationError):
        review(historical=True, **{"verdict": "commented", "provider_request_id": uuid4(), **updates})


def test_historical_comment_preserves_old_commit_without_head_check():
    def handler(request):
        if request.url.path.endswith("/user"):
            return httpx.Response(200, json={"id": 7})
        assert request.url.path.endswith("/reviews")
        if request.method == "GET":
            return httpx.Response(200, json=[])
        payload = json.loads(request.content)
        assert payload["event"] == "COMMENT" and payload["commit_id"] == "a" * 40
        return httpx.Response(200, json={"id": 12, "user": {"id": 7}, **payload})
    client = ForgejoClient("https://forge.test", "test-only", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert client.submit_review("owner/repo", 1, expected_head="a" * 40, body="Earlier findings",
        verdict="commented", historical=True, operation_id="historical")["id"] == 12
