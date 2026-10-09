from __future__ import annotations

"""Bounded Forgejo/Zulip adapters and reconciliation of durable delivery.

Adapters perform no implicit retries. The manager snapshots/claims database
state, closes its transaction, performs HTTP, then reconciles under a fence.
"""

import json
import base64
import binascii
import math
import os
import re
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from urllib.parse import quote, urlsplit
from uuid import UUID, uuid4

import httpx
from sqlalchemy import String, and_, case, cast, exists, func, or_, select, update
from tenacity import wait_random_exponential

from ..auth import Actor, require_project
from .communications import route_message, route_subject_event
from ..errors import DomainError
from ..persistence.records import change, create, emit, get, object_ref, project_of, snapshot, transaction_lock
from ..persistence.schema import tables


class ConnectorFailure(RuntimeError):
    def __init__(self, code: str, *, transient: bool = False, uncertain: bool = False,
                 retry_after: float = 0, message: str | None = None) -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code, self.transient, self.uncertain, self.retry_after = code, transient, uncertain, retry_after


class SecretResolver:
    """Resolve explicit secret:<name> files from this installation only."""

    def __init__(self, state_root: Path) -> None:
        if not state_root.is_absolute():
            raise ValueError("secret state_root must be absolute")
        self.root = state_root / "secrets"

    def __call__(self, reference: str) -> dict[str, str]:
        if not re.fullmatch(r"(?:secret:)?[a-zA-Z0-9_-]{1,100}", reference):
            raise ConnectorFailure("invalid_credential_reference")
        path = self.root / (reference.removeprefix("secret:") + ".json")
        if path.is_symlink() or path.resolve().parent != self.root.resolve():
            raise ConnectorFailure("unsafe_credential_path")
        try:
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 16384:
                    raise ConnectorFailure("credential_file_requires_private_bounded_storage")
                content = stream.read(16385)
                if len(content) > 16384:
                    raise ConnectorFailure("credential_file_requires_private_bounded_storage")
                value = json.loads(content)
        except (OSError, ValueError) as error:
            raise ConnectorFailure("credential_unavailable") from error
        if not isinstance(value, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()):
            raise ConnectorFailure("invalid_credential_file")
        return value


class RemoteClient:
    def __init__(self, endpoint: str, *, client: httpx.Client | None = None,
                 headers: dict[str, str] | None = None, auth: httpx.Auth | None = None) -> None:
        url = urlsplit(endpoint)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ConnectorFailure("invalid_integration_endpoint")
        self.endpoint = endpoint.rstrip("/")
        self.headers, self.auth = headers or {}, auth
        self.authorize_mutation: Callable[[], None] | None = None
        self.should_stop: Callable[[], bool] | None = None
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=httpx.Timeout(15, connect=5),
                                             limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
                                             follow_redirects=False, trust_env=False)

    def request(self, method: str, path: str, *, params=None, data=None, body=None,
                mutating: bool = False, text: bool = False,
                poll: bool = False) -> Any:
        try:
            if self.should_stop is not None and self.should_stop():
                raise ConnectorFailure("connector_stopping", transient=True)
            if mutating and self.authorize_mutation is not None:
                self.authorize_mutation()
            with self.client.stream(method, self.endpoint + path, params=params, data=data, json=body,
                                    headers=self.headers, auth=self.auth,
                                    timeout=httpx.Timeout(10 if poll else 15, connect=5),
                                    follow_redirects=False) as response:
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > 8 * 1024 * 1024:
                        raise ConnectorFailure("remote_response_too_large", uncertain=mutating)
                if not 200 <= response.status_code < 300:
                    code = f"http_{response.status_code}"
                    try:
                        remote_code = json.loads(content).get("code")
                        if remote_code == "BAD_EVENT_QUEUE_ID":
                            code = remote_code
                    except (ValueError, AttributeError):
                        pass
                    try:
                        retry_after = float(response.headers.get("Retry-After", "0"))
                    except ValueError:
                        retry_after = 0
                    # A malformed remote hint must not poison the durable
                    # retry timestamp with NaN/infinity. Use normal backoff.
                    retry_after = max(0, retry_after) if math.isfinite(retry_after) else 0
                    transient = response.status_code in {408, 425, 429} or response.status_code >= 500
                    raise ConnectorFailure(code, transient=transient,
                                           uncertain=mutating and response.status_code >= 500,
                                           retry_after=retry_after)
                if not content:
                    return "" if text else {}
                if text:
                    try:
                        return content.decode("utf-8")
                    except UnicodeDecodeError as error:
                        raise ConnectorFailure("remote_text_not_utf8") from error
                try:
                    result = json.loads(content)
                except ValueError as error:
                    raise ConnectorFailure("malformed_remote_response", uncertain=mutating) from error
                if isinstance(result, dict) and result.get("result") == "error":
                    code = "BAD_EVENT_QUEUE_ID" if result.get("code") == "BAD_EVENT_QUEUE_ID" else "remote_error"
                    raise ConnectorFailure(code, uncertain=mutating)
                return result
        except httpx.ReadTimeout as error:
            if poll:
                raise ConnectorFailure("poll_deadline", transient=True) from error
            raise ConnectorFailure("transport_unavailable", transient=True, uncertain=mutating) from error
        except httpx.TransportError as error:
            raise ConnectorFailure("transport_unavailable", transient=True, uncertain=mutating) from error

    def close(self) -> None:
        if self._owns_client:
            self.client.close()


class ForgejoClient(RemoteClient):
    def __init__(self, endpoint: str, token: str, *, client: httpx.Client | None = None) -> None:
        if not token:
            raise ConnectorFailure("forge_token_missing")
        super().__init__(endpoint, client=client, headers={"Authorization": f"token {token}"})

    @staticmethod
    def repository_path(remote_path: str) -> str:
        pieces = remote_path.split("/")
        if len(pieces) != 2 or any(not value or value in {".", ".."} for value in pieces):
            raise ConnectorFailure("forge_repository_requires_owner_and_name")
        return "/api/v1/repos/" + "/".join(quote(value, safe="") for value in pieces)

    def pages(self, path: str, *, params: dict | None = None, max_pages: int = 100) -> list[dict]:
        result: list[dict] = []
        for page in range(1, max_pages + 1):
            values = self.request("GET", path, params={**(params or {}), "page": page, "limit": 50})
            if not isinstance(values, list) or any(not isinstance(value, dict) for value in values):
                raise ConnectorFailure("malformed_forge_listing")
            result.extend(values)
            if len(values) < 50:
                return result
        raise ConnectorFailure("forge_pagination_limit", transient=True)

    def pull(self, remote_path: str, number: int) -> dict:
        result = self.request("GET", f"{self.repository_path(remote_path)}/pulls/{number}")
        if not isinstance(result, dict) or not result.get("head", {}).get("sha"):
            raise ConnectorFailure("malformed_pull_request")
        return result

    def file_at_commit(self, remote_path: str, commit_oid: str, file_path: str) -> dict:
        from pathlib import PurePosixPath

        path = PurePosixPath(file_path)
        if (not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit_oid) or not file_path
                or path.is_absolute() or ".." in path.parts or str(path) != file_path or "\\" in file_path):
            raise ConnectorFailure("invalid_exact_commit_file")
        result = self.request("GET", self.repository_path(remote_path) + "/contents/" + quote(file_path, safe="/"),
                              params={"ref": commit_oid})
        if not isinstance(result, dict) or result.get("type") != "file" or result.get("encoding") != "base64":
            raise ConnectorFailure("requested_path_is_not_a_regular_file")
        try:
            data = base64.b64decode("".join(result["content"].split()), validate=True)
        except (KeyError, ValueError, TypeError, binascii.Error) as error:
            raise ConnectorFailure("malformed_forge_file") from error
        if len(data) > 4 * 1024**2:
            raise ConnectorFailure("forge_file_too_large")
        try:
            decoded = data.decode("utf-8")
        except UnicodeDecodeError:
            decoded = None
        return {"commit_oid": commit_oid, "path": file_path, "blob_oid": result.get("sha"),
                "size_bytes": len(data), "content_base64": base64.b64encode(data).decode(), "text": decoded}

    def inspect_item(self, remote_path: str, number: int, *, kind: str, view: str,
                     expected_head_oid: str | None, page: int, limit: int, review_id: int | None = None) -> dict:
        if not 1 <= page <= 10000 or not 1 <= limit <= 100:
            raise ConnectorFailure("invalid_inspection_page")
        if view not in {"files", "diff", "comments", "reviews", "review_comments"}:
            raise ConnectorFailure("invalid_inspection_view")
        if kind != "pull_request" and view != "comments":
            raise ConnectorFailure("inspection_requires_pull_request")
        path = self.repository_path(remote_path)
        head = None
        if kind == "pull_request":
            if not isinstance(expected_head_oid, str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", expected_head_oid):
                raise ConnectorFailure("inspection_requires_exact_head")
            head = self.pull(remote_path, number)
            if head["head"]["sha"] != expected_head_oid:
                raise ConnectorFailure("review_head_changed")
        if view == "diff":
            diff = self.request("GET", f"{path}/pulls/{number}.diff", text=True)
            lines = diff.splitlines(keepends=True)
            start = (page - 1) * limit
            result = {"diff": "".join(lines[start:start + limit]), "total_lines": len(lines),
                      "has_more": start + limit < len(lines)}
        else:
            if view == "comments":
                suffix = f"issues/{number}/comments"
            elif view == "review_comments":
                if review_id is None or review_id < 1:
                    raise ConnectorFailure("inspection_requires_review_id")
                suffix = f"pulls/{number}/reviews/{review_id}/comments"
            else:
                suffix = f"pulls/{number}/{view}"
            rows = self.request("GET", f"{path}/{suffix}", params={"page": page, "limit": limit})
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ConnectorFailure("malformed_forge_listing")
            result = {"items": rows, "has_more": len(rows) == limit}
        if head is not None and self.pull(remote_path, number)["head"]["sha"] != expected_head_oid:
            raise ConnectorFailure("review_head_changed")
        return {**result, "head_commit_oid": expected_head_oid if head else None, "page": page, "limit": limit,
                "next_page": page + 1 if result["has_more"] else None}

    def sync_items(self, remote_path: str) -> list[dict]:
        path = self.repository_path(remote_path)
        repository = self.request("GET", path)
        if not isinstance(repository, dict) or any(
                key in repository and not isinstance(repository[key], bool)
                for key in ("has_issues", "has_pull_requests")):
            raise ConnectorFailure("malformed_forge_repository")
        # Mirrors can explicitly disable PRs, whose listing then returns 404.
        # Missing capabilities retain normal listing/error handling.
        issues = ([] if repository.get("has_issues") is False else
                  self.pages(path + "/issues", params={"state": "all", "type": "issues"}))
        pulls = ([] if repository.get("has_pull_requests") is False else
                 self.pages(path + "/pulls", params={"state": "all"}))
        return [dict(value, _kind="issue") for value in issues if not value.get("pull_request")] + [dict(value, _kind="pull_request") for value in pulls]

    def set_label(self, remote_path: str, number: int, label: str, action: str) -> None:
        if action not in {"add", "remove"} or not label or len(label) > 200:
            raise ConnectorFailure("invalid_label_operation")
        path = self.repository_path(remote_path)
        present = self.request("GET", f"{path}/issues/{number}/labels")
        matching = next((value for value in present if value["name"] == label), None)
        if (action == "add" and matching) or (action == "remove" and not matching):
            return
        if action == "remove":
            self.request("DELETE", f"{path}/issues/{number}/labels/{matching['id']}", mutating=True)
            return
        known = next((value for value in self.pages(path + "/labels") if value["name"] == label), None)
        if known is None:
            try:
                known = self.request("POST", path + "/labels", body={"name": label, "color": "6e7781"}, mutating=True)
            except ConnectorFailure as error:
                if error.code != "http_409":
                    raise
                known = next((value for value in self.pages(path + "/labels") if value["name"] == label), None)
        if known is None:
            raise ConnectorFailure("label_creation_not_observed", transient=True)
        self.request("POST", f"{path}/issues/{number}/labels", body={"labels": [known["id"]]}, mutating=True)

    def require_head_checks(self, remote_path: str, number: int, expected_head: str,
                            required_checks: list[str]) -> dict:
        pull = self.pull(remote_path, number)
        if pull["head"]["sha"] != expected_head:
            raise ConnectorFailure("pull_head_changed")
        if pull.get("merged"):
            return pull
        if pull.get("state") != "open":
            raise ConnectorFailure("pull_not_open")
        if required_checks:
            statuses = self.pages(f"{self.repository_path(remote_path)}/statuses/{quote(expected_head, safe='')}")
            latest: dict[str, dict] = {}
            for status in sorted(statuses, key=lambda value: value.get("id", 0), reverse=True):
                latest.setdefault(status["context"], status)
            if any(latest.get(name, {}).get("status") != "success" for name in required_checks):
                raise ConnectorFailure("required_checks_not_successful")
        return pull

    def find_review(self, remote_path: str, number: int, marker: str, expected_head: str) -> dict | None:
        current_user = self.request("GET", "/api/v1/user")
        for value in self.pages(f"{self.repository_path(remote_path)}/pulls/{number}/reviews"):
            if (marker in value.get("body", "") and value.get("commit_id") == expected_head
                    and str(value.get("user", {}).get("id")) == str(current_user["id"])):
                return value
        return None

    def submit_review(self, remote_path: str, number: int, *, expected_head: str,
                      body: str, verdict: str, operation_id: str, reconcile_only: bool = False,
                      comments: list[dict] | None = None, historical: bool = False,
                      expected_base_oid: str | None = None) -> dict:
        if historical and (verdict != "commented" or comments):
            raise ConnectorFailure("invalid_historical_review")
        marker = f"<!-- horizon-operation:{operation_id} -->"
        found = self.find_review(remote_path, number, marker, expected_head)
        if found:
            if expected_base_oid is not None:
                found = dict(found, _horizon_base_oid=self.pull(remote_path, number).get("base", {}).get("sha"))
            return found
        if expected_base_oid is not None:
            pull = self.require_head_checks(remote_path, number, expected_head, [])
            if pull.get("base", {}).get("sha") != expected_base_oid:
                raise ConnectorFailure("review_carry_base_changed")
        if reconcile_only:
            raise ConnectorFailure("review_outcome_uncertain", uncertain=True)
        if not historical:
            self.require_head_checks(remote_path, number, expected_head, [])
        events = {"approved": "APPROVED", "changes_requested": "REQUEST_CHANGES", "commented": "COMMENT"}
        if verdict not in events:
            raise ConnectorFailure("invalid_review_verdict")
        payload = {"event": events[verdict], "commit_id": expected_head, "body": body + "\n\n" + marker}
        if comments:
            payload["comments"] = comments
        result = self.request("POST", f"{self.repository_path(remote_path)}/pulls/{number}/reviews",
                              body=payload, mutating=True)
        return dict(result, _horizon_base_oid=expected_base_oid) if expected_base_oid else result

    def create_item(self, remote_path: str, *, kind: str, title: str, body: str,
                    operation_id: str, head: str | None = None, base: str | None = None,
                    reconcile_only: bool = False, labels: tuple[str, ...] = ()) -> dict:
        if kind not in {"pull_request", "issue"} or (kind == "pull_request" and (not head or not base)):
            raise ConnectorFailure("invalid_forge_creation")
        marker = f"<!-- horizon-operation:{operation_id} -->"
        user = self.request("GET", "/api/v1/user")
        path = self.repository_path(remote_path) + ("/pulls" if kind == "pull_request" else "/issues")
        def finish(value):
            # A lost label response must reconcile this same PR, never create another one.
            for label in labels:
                self.set_label(remote_path, value["number"], label, "add")
            known = {item["name"] for item in value.get("labels", [])}
            return dict(value, _kind=kind, labels=[{"name": name} for name in sorted(known | set(labels))])

        for value in self.pages(path, params={"state": "all"}):
            if marker in (value.get("body") or "") and str(value.get("user", {}).get("id")) == str(user["id"]):
                return finish(value)
        if kind == "pull_request" and any(re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value or "")
                                          for value in (head, base)):
            # Old queued requests may predate branch validation. Reconcile a
            # published marker first, but never submit a commit as a branch.
            raise ConnectorFailure("pull_request_requires_branch_names_not_commit_hashes")
        if reconcile_only:
            raise ConnectorFailure("forge_creation_outcome_uncertain", uncertain=True)
        payload = {"title": title, "body": body + "\n\n" + marker}
        if kind == "pull_request":
            payload.update(head=head, base=base)
        value = self.request("POST", path, body=payload, mutating=True)
        if (not isinstance(value, dict) or not value.get("number") or not value.get("title")
                or not value.get("user", {}).get("id")
                or (kind == "pull_request" and not value.get("head", {}).get("sha"))):
            raise ConnectorFailure("forge_creation_response_incomplete", uncertain=True)
        return finish(value)

    def comment(self, remote_path: str, number: int, *, body: str, operation_id: str,
                reconcile_only: bool = False) -> dict:
        marker = f"<!-- horizon-operation:{operation_id} -->"
        user = self.request("GET", "/api/v1/user")
        path = f"{self.repository_path(remote_path)}/issues/{number}/comments"
        for value in self.pages(path):
            if marker in (value.get("body") or "") and str(value.get("user", {}).get("id")) == str(user["id"]):
                return value
        if reconcile_only:
            raise ConnectorFailure("forge_comment_outcome_uncertain", uncertain=True)
        value = self.request("POST", path, body={"body": body + "\n\n" + marker}, mutating=True)
        if not isinstance(value, dict) or not value.get("id"):
            raise ConnectorFailure("forge_comment_response_incomplete", uncertain=True)
        return value

    def merge_checked(self, remote_path: str, number: int, *, expected_head: str,
                      required_checks: list[str], method: str = "merge", reconcile_only: bool = False,
                      review_remote_id: str | None = None, expected_base: str | None = None,
                      expected_base_oid: str | None = None) -> dict:
        if method not in {"merge", "squash", "rebase", "rebase-merge"}:
            raise ConnectorFailure("invalid_merge_method")
        pull = self.require_head_checks(remote_path, number, expected_head, required_checks)
        if expected_base is not None and pull.get("base", {}).get("ref") != expected_base:
            raise ConnectorFailure("merge_base_changed")
        if pull.get("merged"):
            return pull
        if expected_base_oid is not None and pull.get("base", {}).get("sha") != expected_base_oid:
            raise ConnectorFailure("review_carry_base_changed")
        if reconcile_only:
            raise ConnectorFailure("merge_outcome_uncertain", uncertain=True)
        if review_remote_id is not None:
            review = self.request("GET", f"{self.repository_path(remote_path)}/pulls/{number}/reviews/{quote(review_remote_id, safe='')}")
            if (review.get("commit_id") != expected_head or str(review.get("state", "")).upper() != "APPROVED"
                    or review.get("dismissed") or review.get("stale")):
                raise ConnectorFailure("maintainer_review_no_longer_approved")
        self.request("POST", f"{self.repository_path(remote_path)}/pulls/{number}/merge",
                     body={"Do": method, "head_commit_id": expected_head,
                           "delete_branch_after_merge": False}, mutating=True)
        result = self.pull(remote_path, number)
        if not result.get("merged"):
            raise ConnectorFailure("merge_outcome_uncertain", uncertain=True)
        return result


class ZulipClient(RemoteClient):
    def __init__(self, endpoint: str, email: str, api_key: str, *, client: httpx.Client | None = None) -> None:
        if not email or not api_key:
            raise ConnectorFailure("zulip_credentials_missing")
        self.email = email
        super().__init__(endpoint, client=client, auth=httpx.BasicAuth(email, api_key))

    def register(self) -> dict:
        result = self.request("POST", "/api/v1/register", data={
            "event_types": json.dumps(["message", "update_message", "delete_message"]),
            "fetch_event_types": "[]", "apply_markdown": "false"})
        if (not isinstance(result, dict) or not isinstance(result.get("queue_id"), str)
                or not result["queue_id"] or type(result.get("last_event_id")) is not int):
            raise ConnectorFailure("malformed_zulip_registration")
        return result

    def events(self, queue_id: str, last_event_id: str) -> list[dict]:
        params = {"queue_id": queue_id, "last_event_id": last_event_id, "dont_block": "false"}
        try:
            result = self.request("GET", "/api/v1/events", params=params, poll=True)
        except ConnectorFailure as error:
            if error.code != "poll_deadline":
                raise
            # A blocking connection refreshes Zulip's queue lifetime. Bound its
            # idle wait, then drain/verify the queue rather than mistaking a
            # local timeout for a successful observation or a remote outage.
            result = self.request("GET", "/api/v1/events", params={**params, "dont_block": "true"})
        events = result.get("events") if isinstance(result, dict) else None
        if not isinstance(events, list) or any(not isinstance(event, dict)
                or type(event.get("id")) is not int or not isinstance(event.get("type"), str) for event in events):
            raise ConnectorFailure("malformed_zulip_events")
        return events

    def topic_messages(self, channel_id: str, topic: str, *, max_pages: int = 100, after: int = 0) -> list[dict]:
        narrow = json.dumps([{"operator": "stream", "operand": int(channel_id)},
                             {"operator": "topic", "operand": topic}])
        anchor = after
        result: list[dict] = []
        for _ in range(max_pages):
            response = self.request("GET", "/api/v1/messages", params={"narrow": narrow, "anchor": anchor,
                                    "num_before": 0, "num_after": 100, "include_anchor": "false",
                                    "apply_markdown": "false"})
            messages = response.get("messages") if isinstance(response, dict) else None
            if not isinstance(messages, list) or any(not isinstance(message, dict)
                    or type(message.get("id")) is not int for message in messages):
                raise ConnectorFailure("malformed_zulip_messages")
            if response.get("history_limited"):
                raise ConnectorFailure("zulip_history_unavailable")
            result.extend(messages)
            if response.get("found_newest"):
                return result
            if not messages:
                raise ConnectorFailure("zulip_pagination_stalled", transient=True)
            new_anchor = messages[-1]["id"]
            if new_anchor <= anchor:
                raise ConnectorFailure("zulip_pagination_stalled")
            anchor = new_anchor
        raise ConnectorFailure("zulip_backfill_limit", transient=True)

    def message(self, remote_id: int) -> dict:
        return self.request("GET", f"/api/v1/messages/{remote_id}", params={"apply_markdown": "false"})["message"]

    def topics(self, channel_id: str, *, q: str = "", before: int | None = None, limit: int = 20) -> dict:
        response = self.request("GET", f"/api/v1/users/me/{int(channel_id)}/topics",
                                params={"allow_empty_topic_name": "true"})
        topics = response.get("topics") if isinstance(response, dict) else None
        if not isinstance(topics, list) or any(not isinstance(row, dict) or not isinstance(row.get("name"), str)
                or type(row.get("max_id")) is not int for row in topics):
            raise ConnectorFailure("malformed_zulip_topics")
        values = sorted((row for row in topics if q.casefold() in row["name"].casefold()
                         and (before is None or row["max_id"] < before)), key=lambda row: row["max_id"], reverse=True)
        items = values[:limit]
        return {"items": items, "next_before": items[-1]["max_id"] if len(values) > limit else None}

    def search_messages(self, channel_id: str, *, q: str, topic: str | None = None,
                        before: int | None = None, limit: int = 20) -> dict:
        narrow = [{"operator": "stream", "operand": int(channel_id)}, {"operator": "search", "operand": q}]
        if topic is not None:
            narrow.append({"operator": "topic", "operand": topic})
        response = self.request("GET", "/api/v1/messages", params={"narrow": json.dumps(narrow),
            "anchor": before if before is not None else "newest", "include_anchor": "false",
            "num_before": limit, "num_after": 0, "apply_markdown": "false"})
        values = response.get("messages") if isinstance(response, dict) else None
        if not isinstance(values, list) or len(values) > limit or any(not isinstance(row, dict)
                or type(row.get("id")) is not int or str(row.get("stream_id")) != str(channel_id)
                or not isinstance(row.get("subject"), str) or not isinstance(row.get("content"), str)
                or (topic is not None and row["subject"].casefold() != topic.casefold()) for row in values):
            raise ConnectorFailure("malformed_zulip_search")
        items = [{"remote_id": str(row["id"]), "topic": row["subject"], "excerpt": row["content"][:1200],
                  "excerpt_truncated": len(row["content"]) > 1200, "posted_at": row.get("timestamp"),
                  "author": str(row.get("sender_id", ""))} for row in reversed(values)]
        return {"items": items, "next_before": min(row["id"] for row in values)
                if values and not response.get("found_oldest") else None,
                "history_limited": bool(response.get("history_limited"))}

    def post(self, channel_id: str, topic: str, body: str, operation_id: str, *, reconcile_only: bool = False,
             expected_message_ids: set[str] | None = None,
             expected_message_bodies: dict[str, str] | None = None,
             read_start_remote_id: int | None = None) -> dict:
        marker = f"<!-- horizon-operation:{operation_id} -->"
        start = min([read_start_remote_id, *(int(value) for value in expected_message_ids or ())]) if read_start_remote_id is not None else 0
        messages = self.topic_messages(channel_id, topic, after=max(0, start - 1))
        for message in messages:
            if marker in message.get("content", "") and message.get("sender_email", "").casefold() == self.email.casefold():
                return {"id": message["id"], "result": "success"}
        if reconcile_only:
            raise ConnectorFailure("zulip_post_outcome_uncertain", uncertain=True)
        relevant = ([message for message in messages if message["id"] >= read_start_remote_id
                     or str(message["id"]) in (expected_message_ids or set())]
                    if read_start_remote_id is not None else messages[-100:])
        if expected_message_ids is not None and any(str(message["id"]) not in expected_message_ids for message in relevant):
            raise ConnectorFailure("reply_requires_updated_read_receipts")
        if expected_message_bodies is not None and any(
                expected_message_bodies.get(str(message["id"])) != message.get("content") for message in relevant):
            raise ConnectorFailure("reply_requires_updated_read_receipts")
        if read_start_remote_id is not None and expected_message_ids is not None and (
                {str(message["id"]) for message in relevant} != expected_message_ids):
            raise ConnectorFailure("reply_requires_updated_read_receipts")
        return self.request("POST", "/api/v1/messages", data={"type": "stream", "to": channel_id,
                            "topic": topic, "content": body + "\n\n" + marker}, mutating=True)


class ConnectorManager:
    def __init__(self, database, service, secret_resolver: Callable[[str], dict[str, str]], *,
                 client_factory: Callable[[dict], httpx.Client] | None = None,
                 max_delivery_attempts: int = 20, should_stop: Callable[[], bool] | None = None) -> None:
        self.database, self.service, self.secret_resolver = database, service, secret_resolver
        self.client_factory = client_factory
        self.owner = "connector-" + str(uuid4())
        self.max_delivery_attempts = max_delivery_attempts
        self.should_stop = should_stop

    def remote_client(self, integration: dict, credential_ref: str | None = None) -> ForgejoClient | ZulipClient:
        secret = self.secret_resolver(credential_ref or integration["credential_ref"])
        client = self.client_factory(integration) if self.client_factory else None
        if integration["kind"] == "forge":
            remote = ForgejoClient(integration["endpoint"], secret.get("token", ""), client=client)
        else:
            remote = ZulipClient(integration["endpoint"], secret.get("email", ""), secret.get("api_key", ""), client=client)
        remote.should_stop = self.should_stop
        return remote

    @staticmethod
    def _cursor(conn, integration_id: UUID, consumer: str) -> dict:
        table = tables["connector_cursor"]
        value = conn.execute(select(table).where(table.c.integration_id == integration_id,
                                               table.c.consumer == consumer)).mappings().first()
        return dict(value) if value else create(conn, "connector_cursor", integration_id=integration_id, consumer=consumer)

    def _unavailable(self, integration: dict) -> None:
        with self.database.transaction() as conn:
            transaction_lock(conn)
            cursor = self._cursor(conn, integration["id"], integration["kind"])
            change(conn, "connector_cursor", cursor["id"], status="unavailable")
            if integration["kind"] == "zulip":
                table = tables["discussion"]
                conn.execute(update(table).where(table.c.integration_id == integration["id"]).values(sync_status="unavailable"))

    def sync_all(self, kind: str | None = None) -> dict[str, str]:
        if kind not in {None, "forge", "zulip"}:
            raise ValueError("unknown connector kind")
        with self.database.transaction() as conn:
            query = select(tables["integration"]).where(tables["integration"].c.enabled.is_(True))
            if kind is not None:
                query = query.where(tables["integration"].c.kind == kind)
            integrations = [dict(row) for row in conn.execute(query).mappings()]
        result = {}
        for integration in integrations:
            if self.should_stop is not None and self.should_stop():
                break
            try:
                self.sync_forge(integration) if integration["kind"] == "forge" else self.sync_zulip(integration)
                result[str(integration["id"])] = "current"
            except (ConnectorFailure, KeyError, ValueError, TypeError) as error:
                if not isinstance(error, ConnectorFailure) or error.code not in {
                        "concurrent_connector_observation", "zulip_queue_replaced", "connector_stopping"}:
                    self._unavailable(integration)
                result[str(integration["id"])] = error.code if isinstance(error, ConnectorFailure) else "malformed_observation"
        return result

    @staticmethod
    def _item_values(remote: dict) -> dict:
        pull = remote["_kind"] == "pull_request"
        status = "merged" if pull and remote.get("merged") else "open" if remote.get("state") == "open" else "closed"
        return {"remote_number": int(remote["number"]), "kind": remote["_kind"], "title": remote["title"],
                "status": status, "head_commit_oid": remote.get("head", {}).get("sha") if pull else None,
                "target_branch": remote.get("base", {}).get("ref") if pull else None,
                "author_remote_id": str(remote["user"]["id"]) if remote.get("user") else None,
                "labels": sorted({value["name"] for value in remote.get("labels", [])}),
                "observed_at": datetime.now(timezone.utc)}

    def sync_forge(self, integration: dict) -> None:
        with self.database.transaction() as conn:
            transaction_lock(conn)
            cursor = self._cursor(conn, integration["id"], "forge")
            repositories = [dict(row) for row in conn.execute(select(tables["repository"]).where(
                tables["repository"].c.integration_id == integration["id"],
                tables["repository"].c.archived_at.is_(None))).mappings()]
        client = self.remote_client(integration)
        assert isinstance(client, ForgejoClient)
        try:
            failures = []
            for repository in repositories:
                with self.database.transaction() as conn:
                    transaction_lock(conn)
                    repository_cursor = self._cursor(conn, integration["id"], "forge:" + str(repository["id"]))
                try:
                    observations = client.sync_items(repository["remote_path"] or repository["remote_id"])
                    values_list = [self._item_values(remote) for remote in observations]
                except (ConnectorFailure, KeyError, ValueError, TypeError) as error:
                    if isinstance(error, ConnectorFailure) and error.code == "connector_stopping":
                        raise
                    failures.append(error if isinstance(error, ConnectorFailure) else ConnectorFailure("malformed_observation"))
                    with self.database.transaction() as conn:
                        transaction_lock(conn)
                        if get(conn, "connector_cursor", repository_cursor["id"])["revision"] == repository_cursor["revision"]:
                            change(conn, "connector_cursor", repository_cursor["id"], status="unavailable")
                    continue
                # Release the shared writer lock between bounded batches; a
                # large repository must not starve leases or worker heartbeats.
                for offset in range(0, max(1, len(values_list)), 100):
                    if self.should_stop is not None and self.should_stop():
                        raise ConnectorFailure("connector_stopping", transient=True)
                    with self.database.transaction() as conn:
                        transaction_lock(conn)
                        if get(conn, "connector_cursor", repository_cursor["id"])["revision"] != repository_cursor["revision"]:
                            raise ConnectorFailure("concurrent_connector_observation", transient=True)
                        for values in values_list[offset:offset + 100]:
                            table = tables["forge_item"]
                            old = conn.execute(select(table).where(table.c.repository_id == repository["id"],
                                table.c.kind == values["kind"], table.c.remote_number == values["remote_number"])).mappings().first()
                            if old:
                                changed = [key for key, value in values.items() if key != "observed_at" and old[key] != value]
                                if changed:
                                    row = change(conn, "forge_item", old["id"], **values)
                                    if old["head_commit_oid"] != row["head_commit_oid"] or old["target_branch"] != row["target_branch"]:
                                        self._invalidate_gate(conn, row["id"])
                                else:
                                    # Even an otherwise unchanged terminal item may
                                    # still need its stale review labels reconciled.
                                    row = old
                            else:
                                row = create(conn, "forge_item", repository_id=repository["id"], **values)
                                changed = list(values)
                            # Remote phase labels are presentation, never routing authority.
                            if changed:
                                event = emit(conn, None, repository["project_id"], "forge_item", row, changed,
                                             source="forge:" + str(integration["id"]))
                                route_subject_event(conn, event)
                            # A terminal Forge item must not remain visually awaiting
                            # review.  Queue this through the normal durable delivery
                            # path so a transient Forge outage cannot lose cleanup.
                            from ..review.labels import terminal_label_operation
                            from ..execution.scheduler import Scheduler
                            terminal_label_operation(conn, self.service,
                                                     Scheduler(self.service).system_actor(conn).id, row)
                        complete = offset + 100 >= len(values_list)
                        repository_cursor = change(conn, "connector_cursor", repository_cursor["id"],
                            status="current" if complete else "reconciling",
                            last_synced_at=func.now() if complete else repository_cursor["last_synced_at"])
            with self.database.transaction() as conn:
                transaction_lock(conn)
                if get(conn, "connector_cursor", cursor["id"])["revision"] != cursor["revision"]:
                    raise ConnectorFailure("concurrent_connector_observation", transient=True)
                change(conn, "connector_cursor", cursor["id"], status="unavailable" if failures else "current",
                       last_synced_at=cursor["last_synced_at"] if failures else func.now())
            if failures:
                raise failures[0]
        finally:
            client.close()

    def _claim_operation(self) -> dict | None:
        table = tables["outbox_operation"]
        preceding = table.alias("preceding_delivery")
        def delivery_target(operations):
            payload = operations.c.payload
            # New commits have private operation branches; independent PRs and
            # issues must not queue behind an unrelated uncertain repository write.
            return func.coalesce(payload["forge_item_id"].astext, payload["discussion_id"].astext,
                case((and_(operations.c.kind == "forge_create", payload["kind"].astext == "pull_request"),
                      payload["repository_id"].astext + ":pull:" + payload["head"].astext),
                     (operations.c.kind.in_(("forge_change", "forge_create")), cast(operations.c.id, String)),
                     else_=payload["repository_id"].astext))
        target, preceding_target = delivery_target(table), delivery_target(preceding)
        blocked = exists(select(preceding.c.id).where(
            preceding.c.status.in_(("pending", "running", "uncertain")), preceding_target == target,
            or_(preceding.c.created_at < table.c.created_at,
                and_(preceding.c.created_at == table.c.created_at, preceding.c.id < table.c.id))))
        now = datetime.now(timezone.utc)
        with self.database.transaction() as conn:
            transaction_lock(conn)
            row = conn.execute(select(table).where(
                table.c.kind.in_(("zulip_post", "forge_label", "forge_review", "forge_merge", "forge_create", "forge_comment", "forge_change", "forge_edit")),
                ~blocked,
                or_(and_(table.c.status.in_(("pending", "uncertain")), or_(table.c.retry_at.is_(None), table.c.retry_at <= now)),
                    and_(table.c.status == "running", table.c.lease_expires_at <= now)),
            ).order_by(
                # Historical terminal-label repair is deliberately background
                # housekeeping.  Current reviews, merges, publications and
                # worker messages must pass it when both are ready.
                case((table.c.idempotency_key.like("terminal-review-label:%"), 1), else_=0),
                table.c.created_at, table.c.id,
            ).with_for_update(skip_locked=True).limit(1)).mappings().first()
            if row is None:
                return None
            uncertain = row["status"] in {"uncertain", "running"}
            claimed = change(conn, "outbox_operation", row["id"], status="running", lease_owner=self.owner,
                             lease_epoch=row["lease_epoch"] + 1, lease_expires_at=now + timedelta(seconds=300),
                             retry_count=row["retry_count"] + 1)
            return {**claimed, "_reconcile_only": uncertain}

    @staticmethod
    def _actor(conn, operation: dict) -> Actor:
        principal = get(conn, "principal", operation["actor_principal_id"])
        if principal["disabled_at"] is not None and not operation["_reconcile_only"]:
            raise ConnectorFailure("actor_disabled")
        owner = {key: str(principal[key]) for key in ("username", "host_id", "execution_id", "service_name") if principal[key] is not None}
        return Actor(principal["id"], principal["kind"], owner,
                     "execution_token" if principal["kind"] == "agent" else "api_key")

    @staticmethod
    def _require_delivery_project(conn, operation: dict, actor: Actor, project_id: UUID, role: str) -> None:
        """Deliver an already authorized intent without reviving its API lease.

        Queue insertion checked live authority. Delivery keeps that exact intent
        and its execution's original role, while honoring explicit revocations.
        This helper is private to connector writes; API calls still need a lease.
        """
        from ..auth import ROLE_ORDER
        stored = get(conn, "outbox_operation", operation["id"])
        if (stored["status"] not in {"pending", "running", "uncertain"}
                or any(stored[key] != operation[key] for key in
                       ("actor_principal_id", "project_id", "kind", "payload"))
                or stored["actor_principal_id"] != actor.id or stored["project_id"] != project_id):
            raise ConnectorFailure("delivery_intent_scope_mismatch")
        principal = get(conn, "principal", actor.id)
        if principal["disabled_at"] is not None:
            raise ConnectorFailure("actor_disabled")
        if actor.kind != "agent":
            require_project(conn, actor, project_id, role)
            return
        execution = get(conn, "execution", principal["execution_id"])
        assignment = get(conn, "assignment", execution["assignment_id"])
        run = get(conn, "run", assignment["run_id"])
        pinned = get(conn, "record_revision", execution["assignment_revision_id"])["content"]
        if (execution["status"] in {"stopping", "cancelled"}
                or assignment["status"] in {"stopping", "cancelled"}
                or run["status"] in {"stopping", "cancelled"}):
            raise ConnectorFailure("delivery_authority_revoked")
        if (str(pinned.get("id")) != str(assignment["id"])
                or str(pinned.get("run_id")) != str(run["id"])
                or project_of(conn, "run", run["id"]) != project_id
                or ROLE_ORDER.get(pinned.get("role"), -1) < ROLE_ORDER[role]
                or ROLE_ORDER.get(assignment["role"], -1) < ROLE_ORDER[role]):
            raise ConnectorFailure("delivery_authority_scope_mismatch")

    def _operation_snapshot(self, operation: dict) -> dict:
        with self.database.transaction() as conn:
            actor = self._actor(conn, operation)
            payload = operation["payload"]
            if operation["kind"] == "zulip_post":
                discussion = get(conn, "discussion", payload["discussion_id"])
                if not operation["_reconcile_only"]:
                    self._require_delivery_project(conn, operation, actor, discussion["project_id"], "worker")
                if discussion["sync_status"] != "current":
                    raise ConnectorFailure("discussion_not_current", transient=True)
                read_remote_ids: set[str] = set()
                read_remote_bodies: dict[str, str] = {}
                for receipt in ([] if operation["_reconcile_only"] else payload.get("read_messages", [])):
                    message = get(conn, "message", receipt["id"])
                    if message["discussion_id"] != discussion["id"] or message["revision"] != receipt["revision"]:
                        raise ConnectorFailure("reply_requires_updated_read_receipts")
                    if message["deleted_at"] is None:
                        read_remote_ids.add(message["remote_id"])
                        read_remote_bodies[message["remote_id"]] = message["body"]
                artifact = get(conn, "artifact", payload["body_artifact_id"])
                if artifact["project_id"] != discussion["project_id"] or artifact["kind"] != "blob":
                    raise ConnectorFailure("reply_artifact_scope_mismatch")
                integration = get(conn, "integration", discussion["integration_id"])
                result = {"discussion": discussion, "artifact": artifact, "integration": integration,
                          "actor": actor, "read_remote_ids": read_remote_ids, "read_remote_bodies": read_remote_bodies}
            elif operation["kind"] in {"forge_create", "forge_change"}:
                repository = get(conn, "repository", payload["repository_id"])
                run = get(conn, "run", payload["origin_run_id"])
                if project_of(conn, "run", run["id"]) != repository["project_id"]:
                    raise ConnectorFailure("forge_origin_project_mismatch")
                if not operation["_reconcile_only"]:
                    self._require_delivery_project(conn, operation, actor, repository["project_id"], "worker")
                integration = get(conn, "integration", repository["integration_id"])
                result = {"repository": repository, "integration": integration, "actor": actor}
                if operation["kind"] == "forge_create" and (payload["kind"] == "pull_request" or run.get("orchestration") == "objective"):
                    from ..execution.scheduler import Scheduler
                    policy = Scheduler(self.service).matching_policy(conn, repository["id"], payload["review_phase"])
                    result["creation_labels"] = tuple(sorted(set(
                        ["phase/" + payload["review_phase"]] + (["awaiting-review"] if run.get("orchestration") == "objective" else []) + (policy["attention_labels"] if policy else []))))
                if operation["kind"] == "forge_change":
                    if payload.get("forge_item_id"):
                        if not operation["_reconcile_only"]:
                            self._require_delivery_project(conn, operation, actor, repository["project_id"], "maintainer")
                        item = get(conn, "forge_item", payload["forge_item_id"])
                        if item["repository_id"] != repository["id"] or item["kind"] != "pull_request":
                            raise ConnectorFailure("amendment_scope_mismatch")
                        result["item"] = item
                    artifacts = {}
                    total = 0
                    for file in payload["files"]:
                        if file.get("content_artifact_id"):
                            artifact = get(conn, "artifact", file["content_artifact_id"])
                            if artifact["project_id"] != repository["project_id"] or artifact["kind"] != "blob":
                                raise ConnectorFailure("change_content_artifact_scope_mismatch")
                            size = artifact["content"]["size_bytes"]
                            total += size
                            if size > 1024**2 or total > 8 * 1024**2:
                                raise ConnectorFailure("change_content_too_large")
                            artifacts[file["content_artifact_id"]] = artifact
                    result["artifacts"] = artifacts
            else:
                item = get(conn, "forge_item", payload["forge_item_id"])
                repository = get(conn, "repository", item["repository_id"])
                role = "maintainer" if operation["kind"] in {"forge_review", "forge_merge", "forge_edit"} else "worker"
                if operation["kind"] == "forge_review" and payload.get("reviewer_submission"):
                    invocation = get(conn, "provider_request", payload["provider_request_id"])
                    if (actor.kind != "agent" or str(invocation["execution_id"]) != actor.owner.get("execution_id")
                            or str(invocation["reviewer_descriptor_id"]) != payload.get("reviewer_descriptor_id")
                            or invocation["reason"] != "review"):
                        raise ConnectorFailure("reviewer_submission_scope_mismatch")
                    role = "worker"
                if not operation["_reconcile_only"] or operation["kind"] == "forge_label":
                    self._require_delivery_project(conn, operation, actor, repository["project_id"], role)
                integration = get(conn, "integration", repository["integration_id"])
                result = {"item": item, "repository": repository, "integration": integration, "actor": actor}
                if operation["kind"] == "forge_label":
                    from ..review.demand import label_projection
                    result["label_payload"] = label_projection(conn, item["id"], payload)
                identity_id = payload.get("integration_identity_id")
                if identity_id:
                    identity = get(conn, "integration_identity", identity_id)
                    if identity["integration_id"] != integration["id"] or (not identity["enabled"] and not operation["_reconcile_only"]):
                        raise ConnectorFailure("review_identity_unavailable")
                    result["identity"] = identity
                if operation["kind"] == "forge_merge":
                    gate = get(conn, "review_gate", payload["review_gate_id"])
                    policy = get(conn, "review_policy", gate["policy_id"])
                    revision = get(conn, "record_revision", gate["policy_revision_id"])
                    if not operation["_reconcile_only"] and (gate["forge_item_id"] != item["id"] or gate["status"] != "accepted"
                            or gate["accepted_commit_oid"] != payload["expected_head_oid"]
                            or item["head_commit_oid"] != payload["expected_head_oid"]
                            or not policy["enabled"] or revision["object_revision"] != policy["revision"]):
                        raise ConnectorFailure("merge_gate_stale_or_unaccepted")
                    review = get(conn, "forge_review", gate["maintainer_review_id"]) if gate["maintainer_review_id"] else None
                    if not operation["_reconcile_only"]:
                        from ..review.decisions import postprocessing_review_panel, carried_review_payload
                        panel = postprocessing_review_panel(conn, item, policy)
                        carried = carried_review_payload(conn, item, policy)
                        if (panel["missing"] or panel.get("invalid_carry_forward")
                                or (panel.get('base_commit_oid') and panel['base_commit_oid'] != payload.get('expected_base_oid'))
                                or (carried and carried["expected_base_oid"] != payload.get("expected_base_oid"))):
                            raise ConnectorFailure("review_panel_incomplete_or_stale")
                    result.update(gate=gate, required_checks=policy["required_checks"], review=review)
                elif (operation["kind"] == "forge_review" and payload.get("carry_forward_evidence")
                      and not operation["_reconcile_only"]):
                    from ..review.decisions import _validate_carries
                    revision = get(conn, "record_revision", payload["policy_revision_id"])
                    reference = get(conn, "object_reference", revision["object_id"])
                    policy = get(conn, "review_policy", reference["review_policy_id"])
                    if not policy["enabled"] or item["review_phase"] not in policy["phases"]:
                        raise ConnectorFailure("review_carry_policy_changed")
                    _validate_carries(conn, item, policy, payload["carry_forward_evidence"])
            if not result["integration"]["enabled"]:
                raise ConnectorFailure("integration_disabled")
            return result

    def _deliver(self, operation: dict, state: dict, remote: RemoteClient) -> dict:
        payload = operation["payload"]
        if operation["kind"] == "zulip_post":
            assert isinstance(remote, ZulipClient)
            artifact = state["artifact"]
            body = json.loads(self.service.store.read(artifact["content"]["sha256"], artifact["content"]["size_bytes"]))["body"]
            discussion = state["discussion"]
            require_receipts = payload.get("require_read_receipts", bool(payload.get("read_messages")))
            result = remote.post(discussion["channel_remote_id"], discussion["topic"], body,
                               str(operation["id"]), reconcile_only=operation["_reconcile_only"],
                               expected_message_ids=state["read_remote_ids"] if require_receipts else None,
                               expected_message_bodies=state["read_remote_bodies"] if require_receipts else None,
                               read_start_remote_id=(payload.get("read_start_remote_id") or 0)
                                   if require_receipts and "read_start_remote_id" in payload else None)
            try:
                result["message"] = remote.message(int(result["id"]))
            except ConnectorFailure as error:
                # The send already succeeded. Reconcile its marker before any
                # retry; failure to fetch attribution cannot become a new send.
                raise ConnectorFailure("zulip_post_attribution_pending", transient=True, uncertain=True) from error
            result["authored_content_unchanged"] = (result["message"].get("content") ==
                body + "\n\n<!-- horizon-operation:" + str(operation["id"]) + " -->")
            return result
        assert isinstance(remote, ForgejoClient)
        repository = state["repository"]
        path = repository["remote_path"] or repository["remote_id"]
        if operation["kind"] == "forge_change":
            from .forge_change_transport import change_files
            files = []
            for value in payload["files"]:
                file = {key: value[key] for key in ("operation", "path", "sha") if value.get(key) is not None}
                if value.get("content_artifact_id"):
                    artifact = state["artifacts"][value["content_artifact_id"]]
                    content = self.service.store.read(artifact["content"]["sha256"], artifact["content"]["size_bytes"])
                    file["content"] = base64.b64encode(content).decode()
                files.append(file)
            branch = None
            if payload.get("forge_item_id"):
                pull = remote.request("GET", f"{remote.repository_path(path)}/pulls/{state['item']['remote_number']}")
                head = pull.get("head", {})
                branch = head.get("ref")
                if (pull.get("state") != "open" or not branch or branch == state["repository"]["default_branch"]
                        or head.get("repo", {}).get("id") != pull.get("base", {}).get("repo", {}).get("id")
                        or not head.get("repo", {}).get("id")):
                    raise ConnectorFailure("amendment_requires_open_same_repository_branch")
            return change_files(remote, path, base_commit_oid=payload["base_commit_oid"], message=payload["message"],
                                files=files, operation_id=str(operation["id"]), reconcile_only=operation["_reconcile_only"], branch=branch)
        if operation["kind"] == "forge_create":
            return remote.create_item(path, kind=payload["kind"], title=payload["title"], body=payload["body"],
                                      head=payload.get("head"), base=payload.get("base"), operation_id=str(operation["id"]),
                                      reconcile_only=operation["_reconcile_only"], labels=state.get("creation_labels", ()))
        item = state["item"]
        if operation["kind"] == "forge_edit":
            url = f"{remote.repository_path(path)}/pulls/{item['remote_number']}"
            pull = remote.request("GET", url)
            if pull.get("head", {}).get("sha") != payload["expected_head_oid"] or pull.get("merged"):
                raise ConnectorFailure("edit_head_changed_or_merged")
            desired = {key: payload[key] for key in ("base", "state") if payload.get(key) is not None}
            actual = {"base": pull.get("base", {}).get("ref"), "state": pull.get("state")}
            if not all(actual[key] == value for key, value in desired.items()):
                if actual["base"] != payload["expected_base"]:
                    raise ConnectorFailure("edit_base_changed")
                remote.request("PATCH", url, body=desired, mutating=True)
                pull = remote.request("GET", url)
            if (pull.get("head", {}).get("sha") != payload["expected_head_oid"]
                    or any((pull.get("base", {}).get("ref") if key == "base" else pull.get(key)) != value
                           for key, value in desired.items())):
                raise ConnectorFailure("edit_outcome_changed", uncertain=True)
            return dict(pull, _kind="pull_request")
        if operation["kind"] == "forge_comment":
            return remote.comment(path, item["remote_number"], body=payload["body"], operation_id=str(operation["id"]),
                                  reconcile_only=operation["_reconcile_only"])
        if operation["kind"] == "forge_label":
            payload = state.get("label_payload", payload)
            for action, field in (("remove", "remove"), ("add", "add")):
                for label in dict.fromkeys(payload[field]):
                    remote.set_label(path, item["remote_number"], label, action)
            observed = remote.request("GET", f"{remote.repository_path(path)}/issues/{item['remote_number']}/labels")
            return {"labels": sorted({label["name"] for label in observed})}
        if operation["kind"] == "forge_review":
            return remote.submit_review(path, item["remote_number"], expected_head=payload["commit_oid"],
                                        body=payload["summary"], verdict=payload["verdict"], operation_id=str(operation["id"]),
                                        reconcile_only=operation["_reconcile_only"], comments=payload.get("comments"),
                                        historical=payload.get("historical", False),
                                        **({"expected_base_oid": payload["expected_base_oid"]} if payload.get("expected_base_oid") else {}))
        return remote.merge_checked(path, item["remote_number"], expected_head=payload["expected_head_oid"],
                                    required_checks=state["required_checks"], reconcile_only=operation["_reconcile_only"],
                                    review_remote_id=state["review"]["remote_id"] if state.get("review") else None,
                                    expected_base=payload.get("expected_base"),
                                    **({"expected_base_oid": payload["expected_base_oid"]} if payload.get("expected_base_oid") else {}))

    def _record_review(self, conn, operation: dict, state: dict, remote: dict) -> UUID:
        payload, item, actor = operation["payload"], state["item"], state["actor"]
        if payload.get("review_plan"):
            payload = {**payload, "review_base_verified": remote.get("_horizon_base_oid") == payload.get("expected_base_oid")}
            change(conn, "outbox_operation", operation["id"], payload=payload)
            operation = {**operation, "payload": payload}
        if payload.get("expected_base_oid") and remote.get("_horizon_base_oid") != payload["expected_base_oid"]:
            state = dict(state, carry_base_changed=True)
        table = tables["forge_review"]
        previous = conn.execute(select(table).where(table.c.forge_item_id == item["id"],
                                                    table.c.remote_id == str(remote["id"]))).mappings().first()
        if previous:
            self._activate_gate(conn, operation, state, dict(previous))
            return object_ref(conn, "forge_review", previous["id"])
        reviewer_id = str(remote.get("user", {}).get("id", ""))
        if not reviewer_id or remote.get("commit_id") != payload["commit_oid"]:
            raise ConnectorFailure("review_response_identity_or_head_missing")
        if state.get("identity") and reviewer_id != state["identity"]["remote_user_id"]:
            raise ConnectorFailure("remote_reviewer_identity_mismatch")
        verdicts = {"APPROVED": "approved", "REQUEST_CHANGES": "changes_requested", "REQUESTED_CHANGES": "changes_requested",
                    "COMMENT": "commented", "COMMENTED": "commented"}
        verdict = "dismissed" if remote.get("dismissed") else verdicts.get(str(remote.get("state", "")).upper())
        if verdict is None:
            raise ConnectorFailure("unknown_remote_review_verdict")
        review = create(conn, "forge_review", forge_item_id=item["id"], remote_id=str(remote["id"]),
                        reviewer_remote_id=reviewer_id, reviewer_principal_id=actor.id,
                        reviewer_descriptor_id=UUID(payload["reviewer_descriptor_id"]) if payload.get("reviewer_descriptor_id") else None,
                        reviewer_descriptor_revision_id=UUID(payload["reviewer_descriptor_revision_id"]) if payload.get("reviewer_descriptor_revision_id") else None,
                        provider_request_id=UUID(payload["provider_request_id"]) if payload.get("provider_request_id") else None,
                        integration_identity_id=state.get("identity", {}).get("id"),
                        reviewer_functions=[], verdict=verdict, summary=payload["summary"],
                        commit_oid=payload["commit_oid"], observed_at=func.now())
        event = emit(conn, actor.id, state["repository"]["project_id"], "forge_review", review, ["verdict", "commit_oid"])
        route_subject_event(conn, event)
        # Report delivery is evidence, not a native process completion receipt.
        self._activate_gate(conn, operation, state, review)
        if payload.get("reviewer_descriptor_id"):
            from ..review.labels import queue_state
            descriptor = get(conn, "reviewer_descriptor", payload["reviewer_descriptor_id"])
            request = get(conn, "provider_request", payload["provider_request_id"]) if payload.get("provider_request_id") else None
            label = "historical" if payload.get("historical") else {
                "approved": "ok", "changes_requested": "requesting-changes"}.get(verdict, "commented")
            if label == "ok":
                from ..review.decisions import _current_approval
                revision = get(conn, "record_revision", payload["policy_revision_id"]) if payload.get("policy_revision_id") else None
                policy = get(conn, "review_policy", revision["content"]["id"]) if revision else None
                if not policy or not _current_approval(conn, review, payload, get(conn, "forge_item", item["id"]), policy):
                    label = "commented"
            if item["status"] in {"merged", "closed", "resolved"}:
                # A review that was prepared for an open head may arrive after
                # the PR reached a terminal state.  Preserve the Forge review,
                # but never resurrect an active per-reviewer label.
                from ..review.labels import terminal_label_operation
                terminal_label_operation(conn, self.service, actor.id, item)
            else:
                queue_state(conn, self.service, actor.id, item, descriptor, label, str(operation["id"]), request)
        return object_ref(conn, "forge_review", review["id"])

    @staticmethod
    def _activate_gate(conn, operation: dict, state: dict, review: dict) -> None:
        payload = operation["payload"]
        if (payload.get("reviewer_descriptor_id") and not payload.get("historical")
                and (review["verdict"] in {"changes_requested", "commented", "dismissed"}
                     or not payload.get("provider_request_id"))):
            gate_table = tables["review_gate"]
            existing = conn.execute(select(gate_table).where(
                gate_table.c.forge_item_id == state["item"]["id"], gate_table.c.status == "accepted")).mappings().first()
            if existing:
                from ..review.decisions import postprocessing_review_panel
                item = get(conn, "forge_item", state["item"]["id"])
                policy = get(conn, "review_policy", existing["policy_id"])
                panel = postprocessing_review_panel(conn, item, policy)
                if panel["missing"] or panel.get("invalid_carry_forward"):
                    gate = change(conn, "review_gate", existing["id"], status="pending",
                        accepted_commit_oid=None, maintainer_review_id=None, evaluated_at=func.now())
                    emit(conn, state["actor"].id, state["repository"]["project_id"], "review_gate", gate,
                         ["status", "accepted_commit_oid"])
        if review["verdict"] != "approved" or payload.get("reviewer_descriptor_id"):
            return
        from ..review.decisions import review_readiness
        item = get(conn, "forge_item", state["item"]["id"])
        readiness = review_readiness(conn, item, delivered_payload=payload)
        reasons = []
        revision = None
        if not payload.get("policy_revision_id"):
            reasons.append({"code": "review_policy_pin_missing"})
        else:
            revision = get(conn, "record_revision", payload["policy_revision_id"])
            reference = get(conn, "object_reference", revision["object_id"])
            if (reference["kind"] != "review_policy" or str(reference["review_policy_id"]) != readiness["policy_id"]
                    or revision["object_revision"] != readiness["policy_revision"]):
                reasons.append({"code": "review_policy_pin_stale"})
        if payload.get("historical"):
            reasons.append({"code": "historical_review"})
        if item["head_commit_oid"] != review["commit_oid"] or item["status"] != "open":
            reasons.append({"code": "review_head_changed", "reviewed_head": review["commit_oid"]})
        if payload.get("target_branch") != item["target_branch"]:
            reasons.append({"code": "review_target_changed"})
        if state.get("carry_base_changed"):
            reasons.append({"code": "review_base_changed"})
        readiness = {**readiness, "ready": readiness["ready"] and not reasons,
                     "blockers": [*readiness["blockers"], *reasons]}
        if not readiness["ready"]:
            change(conn, "outbox_operation", operation["id"], payload={**payload,
                "gate_result": {"status": "blocked", "remote_review_delivered": True, "review_readiness": readiness}})
            existing = conn.execute(select(tables["review_gate"]).where(
                tables["review_gate"].c.forge_item_id == item["id"])).mappings().first()
            if (not reasons and existing and existing["status"] == "accepted"
                    and existing["accepted_commit_oid"] == review["commit_oid"]):
                change(conn, "review_gate", existing["id"], status="pending",
                       accepted_commit_oid=None, maintainer_review_id=None,
                       evaluated_at=func.now())
            return
        table = tables["review_gate"]
        existing = conn.execute(select(table).where(table.c.forge_item_id == item["id"])).mappings().first()
        values = {"policy_id": UUID(readiness["policy_id"]), "policy_revision_id": revision["id"], "maintainer_review_id": review["id"],
                  "status": "accepted", "accepted_commit_oid": review["commit_oid"], "evaluated_at": func.now()}
        gate = change(conn, "review_gate", existing["id"], **values) if existing else create(conn, "review_gate", forge_item_id=item["id"], **values)
        emit(conn, state["actor"].id, state["repository"]["project_id"], "review_gate", gate, ["status", "accepted_commit_oid"])
        change(conn, "outbox_operation", operation["id"], payload={**payload,
            "gate_result": {"status": "accepted", "remote_review_delivered": True, "review_gate_id": str(gate["id"]),
                            "review_readiness": readiness}})

    def _settle_operation(self, operation: dict, state: dict | None, result: dict | None,
                          failure: ConnectorFailure | None) -> str:
        with self.database.transaction() as conn:
            transaction_lock(conn)
            current = get(conn, "outbox_operation", operation["id"], lock=True)
            if (current["lease_owner"] != self.owner or current["lease_epoch"] != operation["lease_epoch"]
                    or current["lease_expires_at"] is None or current["lease_expires_at"] <= datetime.now(timezone.utc)):
                raise ConnectorFailure("delivery_fenced")
            result_ref = None
            if failure is None and state is not None:
                if operation["kind"] == "forge_change":
                    content = {"repository_id": str(state["repository"]["id"]), "commit_oid": result["commit_oid"]}
                    table = tables["artifact"]
                    artifact = conn.execute(select(table).where(table.c.project_id == state["repository"]["project_id"],
                        table.c.kind == "commit", table.c.content == content)).mappings().first()
                    principal = get(conn, "principal", operation["actor_principal_id"])
                    if artifact is None:
                        artifact = create(conn, "artifact", project_id=state["repository"]["project_id"], kind="commit",
                                          created_by_execution_id=principal["execution_id"], content=content)
                    if principal["execution_id"]:
                        from sqlalchemy.dialects.postgresql import insert
                        assignment_id = get(conn, "execution", principal["execution_id"])["assignment_id"]
                        conn.execute(insert(tables["assignment_artifact"]).values(assignment_id=assignment_id,
                                     artifact_id=artifact["id"]).on_conflict_do_nothing())
                        create(conn, "publication", artifact_id=artifact["id"], requested_by_assignment_id=assignment_id,
                               target={"kind": "git", "repository_id": str(state["repository"]["id"]),
                                       "ref_name": "refs/heads/" + result["branch"], "expected_old_oid": None},
                               status="verified", verified_at=func.now())
                    create(conn, "artifact_location", artifact_id=artifact["id"],
                           locator=state["integration"]["endpoint"].rstrip("/") + "/" + (state["repository"]["remote_path"] or state["repository"]["remote_id"])
                                   + "/commit/" + result["commit_oid"], verified_at=func.now())
                    result_ref = object_ref(conn, "artifact", artifact["id"])
                    emit(conn, state["actor"].id, state["repository"]["project_id"], "artifact", dict(artifact), ["content"])
                    if payload_id := operation["payload"].get("forge_item_id"):
                        item = change(conn, "forge_item", payload_id, head_commit_oid=result["commit_oid"], observed_at=func.now())
                        self._invalidate_gate(conn, item["id"])
                        route_subject_event(conn, emit(conn, state["actor"].id, state["repository"]["project_id"],
                            "forge_item", item, ["head_commit_oid"]))
                elif operation["kind"] == "forge_create":
                    payload = operation["payload"]
                    values = self._item_values(result or {})
                    table = tables["forge_item"]
                    existing = conn.execute(select(table).where(table.c.repository_id == state["repository"]["id"],
                        table.c.kind == values["kind"], table.c.remote_number == values["remote_number"])).mappings().first()
                    provenance = {"origin_run_id": UUID(payload["origin_run_id"]), "review_phase": payload["review_phase"]}
                    if existing and any(existing[key] is not None and existing[key] != value for key, value in provenance.items()):
                        raise ConnectorFailure("forge_creation_provenance_conflict")
                    item = change(conn, "forge_item", existing["id"], **values, **provenance) if existing else create(
                        conn, "forge_item", repository_id=state["repository"]["id"], **values, **provenance)
                    result_ref = object_ref(conn, "forge_item", item["id"])
                    event = emit(conn, state["actor"].id, state["repository"]["project_id"], "forge_item", item, list(values) + list(provenance))
                    route_subject_event(conn, event)
                elif operation["kind"] == "forge_review":
                    result_ref = self._record_review(conn, operation, state, result or {})
                elif operation["kind"] == "forge_edit":
                    item = change(conn, "forge_item", state["item"]["id"], **self._item_values(result))
                    self._invalidate_gate(conn, item["id"])
                    result_ref = object_ref(conn, "forge_item", item["id"])
                    route_subject_event(conn, emit(conn, state["actor"].id, state["repository"]["project_id"],
                        "forge_item", item, ["target_branch", "status"]))
                    from ..review.labels import terminal_label_operation
                    terminal_label_operation(conn, self.service, state["actor"].id, item)
                elif operation["kind"] == "forge_merge":
                    item = get(conn, "forge_item", state["item"]["id"])
                    item = change(conn, "forge_item", item["id"], status="merged", observed_at=func.now())
                    result_ref = object_ref(conn, "forge_item", item["id"])
                    event = emit(conn, state["actor"].id, state["repository"]["project_id"], "forge_item", item, ["status"])
                    route_subject_event(conn, event)
                    from ..review.labels import terminal_label_operation
                    terminal_label_operation(conn, self.service, state["actor"].id, item)
                elif operation["kind"] in {"forge_label", "forge_comment"}:
                    result_ref = object_ref(conn, "forge_item", state["item"]["id"])
                    if operation["kind"] == "forge_label":
                        item = change(conn, "forge_item", state["item"]["id"], labels=result["labels"])
                        if "review_generation" in operation["payload"]:
                            from ..review.demand import repair_label
                            repair_label(conn, item, state["actor"].id, operation["id"])
                        event = emit(conn, state["actor"].id, state["repository"]["project_id"], "forge_item", item, ["labels"])
                        route_subject_event(conn, event)
                    if operation["kind"] == "forge_comment":
                        event = emit(conn, state["actor"].id, state["repository"]["project_id"], "forge_item",
                                     state["item"], ["comments"])
                        route_subject_event(conn, event)
                elif operation["kind"] == "zulip_post":
                    row = self._upsert_message(conn, state["discussion"], result["message"],
                        author_principal_id=operation["actor_principal_id"],
                        source_assignment_id=operation["payload"].get("source_assignment_id"))
                    result_ref = object_ref(conn, "message", row["id"])
                    if row["source_assignment_id"] and result.get("authored_content_unchanged"):
                        from sqlalchemy.dialects.postgresql import insert as pg_insert
                        conn.execute(pg_insert(tables["message_read"]).values(
                            assignment_id=row["source_assignment_id"], message_id=row["id"],
                            message_revision=row["revision"], read_at=func.now()).on_conflict_do_nothing())
                status, retry_at, error = "completed", None, None
            else:
                assert failure is not None
                deferred = failure.code in {"discussion_not_current", "connector_stopping"}
                uncertain = failure.uncertain or operation.get("_reconcile_only", False)
                exhausted = operation["retry_count"] >= self.max_delivery_attempts and not deferred
                status = "uncertain" if uncertain else "pending" if failure.transient and not exhausted else "failed"
                deferred_attempts = (operation.get("failure") or {}).get("deferred_attempts", 0) + 1 if deferred else 0
                delay = max(failure.retry_after, 5 if deferred else 0, wait_random_exponential(multiplier=2, max=300)(
                    SimpleNamespace(attempt_number=min(32, deferred_attempts if deferred else operation["retry_count"]))))
                # Uncertain side effects stay visible and reconciliable; they are never resent blindly.
                retry_at = datetime.now(timezone.utc) + timedelta(seconds=max(delay, 300) if exhausted else delay)
                error = {"kind": "transport", "code": "outcome_uncertain" if uncertain else "connector_failure",
                         "message": str(failure)}
                if deferred:
                    error["deferred_attempts"] = deferred_attempts
            settled = change(conn, "outbox_operation", operation["id"], status=status, retry_at=retry_at, failure=error,
                             result_ref_id=result_ref, lease_owner=None, lease_expires_at=None,
                             retry_count=operation["retry_count"] - int(failure is not None and failure.code in {
                                 "discussion_not_current", "connector_stopping"}))
            if status == "failed":
                from ..execution.notifications import delivery_failed
                delivery_failed(conn, settled)
            return status

    def _invalidate_gate(self, conn, item_id):
        gate = tables["review_gate"]
        conn.execute(update(gate).where(gate.c.forge_item_id == item_id).values(status="stale",
            accepted_commit_oid=None, maintainer_review_id=None, evaluated_at=func.now(),
            revision=gate.c.revision + 1, updated_at=func.now()))
        item = get(conn, "forge_item", item_id)
        slugs = {label.split("/")[1] for label in item["labels"] if label.startswith("review/") and label.count("/") == 2
                 and label.rsplit("/", 1)[1] in {"ok", "requesting-changes", "commented"}}
        if slugs:
            from ..review.labels import queue_state
            from ..execution.scheduler import Scheduler
            actor = Scheduler(self.service).system_actor(conn)
            descriptor = tables["reviewer_descriptor"]
            project_id = get(conn, "repository", item["repository_id"])["project_id"]
            for row in conn.execute(select(descriptor).where(descriptor.c.project_id == project_id,
                    descriptor.c.slug.in_(slugs))).mappings():
                queue_state(conn, self.service, actor.id, item, dict(row), "historical", f"{item_id}:{item['revision']}:{row['id']}")

    def dispatch_one(self) -> str | None:
        if self.should_stop is not None and self.should_stop():
            return None
        operation = self._claim_operation()
        if operation is None:
            return None
        state = None
        remote = None
        try:
            state = self._operation_snapshot(operation)
            remote = self.remote_client(state["integration"], state.get("identity", {}).get("credential_ref"))
            remote.authorize_mutation = lambda: self._authorize_mutation(operation)
            result = self._deliver(operation, state, remote)
            return self._settle_operation(operation, state, result, None)
        except ConnectorFailure as failure:
            if operation["kind"] == "forge_label" and failure.uncertain:
                failure = ConnectorFailure(failure.code, transient=True, retry_after=failure.retry_after)
            return self._settle_operation(operation, state, None, failure)
        except DomainError as error:
            return self._settle_operation(operation, state, None, ConnectorFailure(error.code))
        finally:
            if remote is not None:
                remote.close()

    def _authorize_mutation(self, operation: dict) -> None:
        with self.database.transaction() as conn:
            current = get(conn, "outbox_operation", operation["id"])
            if (current["status"] != "running" or current["lease_owner"] != self.owner
                    or current["lease_epoch"] != operation["lease_epoch"]
                    or current["lease_expires_at"] is None or current["lease_expires_at"] <= datetime.now(timezone.utc)):
                raise ConnectorFailure("delivery_fenced")
        self._operation_snapshot(operation)

    @staticmethod
    def _upsert_message(conn, discussion: dict, remote: dict, *, author_principal_id=None, source_assignment_id=None) -> dict:
        table = tables["message"]
        existing = conn.execute(select(table).where(table.c.discussion_id == discussion["id"],
                                                    table.c.remote_id == str(remote["id"]))).mappings().first()
        values = {"remote_author_id": str(remote["sender_id"]), "body": remote["content"],
                  "posted_at": datetime.fromtimestamp(remote["timestamp"], timezone.utc),
                  "edited_at": datetime.fromtimestamp(remote["last_edit_timestamp"], timezone.utc) if remote.get("last_edit_timestamp") else None,
                  "deleted_at": None}
        if author_principal_id:
            values["author_principal_id"] = author_principal_id
        if source_assignment_id:
            values["source_assignment_id"] = UUID(str(source_assignment_id))
        if existing:
            changed = [key for key, value in values.items() if existing[key] != value]
            if not changed:
                return dict(existing)
            row = change(conn, "message", existing["id"], **values)
        else:
            row = create(conn, "message", discussion_id=discussion["id"], remote_id=str(remote["id"]), **values)
            changed = list(values)
        event = emit(conn, None, discussion["project_id"], "message", row, changed,
                     source="zulip:" + str(discussion["integration_id"]))
        route_message(conn, event, discussion["id"])
        return row

    @staticmethod
    def _delete_message(conn, discussion: dict, remote_id: int | str) -> None:
        table = tables["message"]
        old = conn.execute(select(table).where(table.c.discussion_id == discussion["id"],
            table.c.remote_id == str(remote_id), table.c.deleted_at.is_(None))).mappings().first()
        if old:
            row = change(conn, "message", old["id"], deleted_at=func.now())
            event = emit(conn, None, discussion["project_id"], "message", row, ["deleted_at"],
                         source="zulip:" + str(discussion["integration_id"]))
            route_message(conn, event, discussion["id"])

    def sync_zulip(self, integration: dict) -> None:
        for attempt in range(2):
            try:
                self._sync_zulip(integration)
                return
            except ConnectorFailure as error:
                if error.code != "BAD_EVENT_QUEUE_ID":
                    raise
                if attempt:
                    raise ConnectorFailure("zulip_queue_replaced", transient=True) from error

    def _sync_zulip(self, integration: dict) -> None:
        with self.database.transaction() as conn:
            transaction_lock(conn)
            cursor = self._cursor(conn, integration["id"], "zulip")
            discussions = [dict(row) for row in conn.execute(select(tables["discussion"]).where(
                tables["discussion"].c.integration_id == integration["id"])).mappings()]
        client = self.remote_client(integration)
        assert isinstance(client, ZulipClient)
        try:
            if not cursor["queue_remote_id"]:
                registered = client.register()
                with self.database.transaction() as conn:
                    transaction_lock(conn)
                    if get(conn, "connector_cursor", cursor["id"])["revision"] != cursor["revision"]:
                        raise ConnectorFailure("concurrent_connector_observation", transient=True)
                    cursor = change(conn, "connector_cursor", cursor["id"], queue_remote_id=registered["queue_id"],
                                    last_event_remote_id=str(registered["last_event_id"]), status="reconciling")
            reconcile_all = cursor["status"] != "current"
            for discussion in discussions:
                if reconcile_all or discussion["sync_status"] != "current":
                    messages = client.topic_messages(discussion["channel_remote_id"], discussion["topic"])
                    with self.database.transaction() as conn:
                        transaction_lock(conn)
                        if get(conn, "connector_cursor", cursor["id"])["revision"] != cursor["revision"]:
                            raise ConnectorFailure("concurrent_connector_observation", transient=True)
                        observed = {str(message["id"]) for message in messages}
                        table = tables["message"]
                        known = conn.execute(select(table.c.remote_id).where(table.c.discussion_id == discussion["id"],
                                                                            table.c.deleted_at.is_(None))).scalars()
                        deleted = [remote_id for remote_id in known if remote_id not in observed]
                    changes = [(message, None) for message in messages] + [(None, remote_id) for remote_id in deleted]
                    for offset in range(0, max(1, len(changes)), 100):
                        if self.should_stop is not None and self.should_stop():
                            raise ConnectorFailure("connector_stopping", transient=True)
                        with self.database.transaction() as conn:
                            transaction_lock(conn)
                            if get(conn, "connector_cursor", cursor["id"])["revision"] != cursor["revision"]:
                                raise ConnectorFailure("concurrent_connector_observation", transient=True)
                            for message, remote_id in changes[offset:offset + 100]:
                                if message is None:
                                    self._delete_message(conn, discussion, remote_id)
                                else:
                                    self._upsert_message(conn, discussion, message)
                            cursor = change(conn, "connector_cursor", cursor["id"], status="reconciling")
            try:
                events = client.events(cursor["queue_remote_id"], cursor["last_event_remote_id"] or "-1")
            except ConnectorFailure as error:
                if error.code == "BAD_EVENT_QUEUE_ID":
                    with self.database.transaction() as conn:
                        transaction_lock(conn)
                        if get(conn, "connector_cursor", cursor["id"])["revision"] == cursor["revision"]:
                            change(conn, "connector_cursor", cursor["id"], queue_remote_id=None,
                                   last_event_remote_id=None, status="reconciling")
                            conn.execute(update(tables["discussion"]).where(
                                tables["discussion"].c.integration_id == integration["id"]
                            ).values(sync_status="reconciling"))
                raise
            observations: list[tuple[dict, dict | None]] = []
            for event in events:
                if event.get("type") == "message":
                    observations.append((event, event["message"]))
                elif event.get("type") == "update_message":
                    for remote_id in event.get("message_ids", [event.get("message_id")]):
                        if remote_id is not None:
                            try:
                                observations.append((event, client.message(int(remote_id))))
                            except ConnectorFailure as error:
                                if error.code != "http_404":
                                    raise
                                observations.append(({"type": "delete_message", "message_ids": [remote_id]}, None))
                elif event.get("type") == "delete_message":
                    observations.append((event, None))
            by_id = {discussion["id"]: discussion for discussion in discussions}
            by_topic: dict[tuple[str, str], list[dict]] = {}
            for discussion in discussions:
                by_topic.setdefault((discussion["channel_remote_id"], discussion["topic"]), []).append(discussion)
            for offset in range(0, max(1, len(observations)), 100):
                if self.should_stop is not None and self.should_stop():
                    raise ConnectorFailure("connector_stopping", transient=True)
                with self.database.transaction() as conn:
                    transaction_lock(conn)
                    latest_cursor = get(conn, "connector_cursor", cursor["id"], lock=True)
                    if latest_cursor["queue_remote_id"] != cursor["queue_remote_id"] or latest_cursor["revision"] != cursor["revision"]:
                        raise ConnectorFailure("zulip_queue_replaced", transient=True)
                    batch = observations[offset:offset + 100]
                    remote_ids = {str(remote_id) for event, message in batch for remote_id in (
                        [message["id"]] if message is not None else event.get("message_ids", [event.get("message_id")]))
                        if remote_id is not None}
                    table = tables["message"]
                    memberships: dict[str, set[UUID]] = {}
                    if remote_ids:
                        for existing in conn.execute(select(table.c.remote_id, table.c.discussion_id).where(
                                table.c.remote_id.in_(remote_ids), table.c.discussion_id.in_(by_id),
                                table.c.deleted_at.is_(None))).mappings():
                            memberships.setdefault(existing["remote_id"], set()).add(existing["discussion_id"])
                    for event, message in batch:
                        if message is not None:
                            old_key = (str(event.get("stream_id")), event.get("orig_subject"))
                            new_key = (str(message.get("stream_id")), message.get("subject"))
                            # Only a confirmed whole-topic rename within the same
                            # channel preserves identity. Splits/merges must not
                            # silently repoint all subscribers to a different scope.
                            if (event.get("propagate_mode") == "change_all" and old_key != new_key
                                    and old_key[0] == new_key[0] and event.get("subject") == new_key[1]
                                    and old_key in by_topic and new_key not in by_topic):
                                renamed = []
                                for old in by_topic.pop(old_key):
                                    updated = change(conn, "discussion", old["id"], topic=new_key[1])
                                    by_id[old["id"]] = updated
                                    renamed.append(updated)
                                    emit(conn, None, old["project_id"], "discussion", updated, ["topic"],
                                         source="zulip:" + str(integration["id"]))
                                by_topic[new_key] = renamed
                            remote_id = str(message["id"])
                            matching = by_topic.get((str(message.get("stream_id")), message.get("subject")), [])
                            destinations = {discussion["id"] for discussion in matching}
                            for discussion_id in memberships.get(remote_id, set()) - destinations:
                                self._delete_message(conn, by_id[discussion_id], remote_id)
                            for discussion in matching:
                                self._upsert_message(conn, discussion, message)
                            memberships[remote_id] = destinations
                        else:
                            for remote_id in event.get("message_ids", [event.get("message_id")]):
                                for discussion_id in memberships.pop(str(remote_id), set()):
                                    self._delete_message(conn, by_id[discussion_id], remote_id)
                    complete = offset + 100 >= len(observations)
                    last_event = max([int(cursor["last_event_remote_id"] or -1), *[int(event["id"]) for event in events]])
                    cursor = change(conn, "connector_cursor", cursor["id"], status="current" if complete else "reconciling",
                        last_event_remote_id=str(last_event) if complete else cursor["last_event_remote_id"],
                        last_synced_at=func.now() if complete else cursor["last_synced_at"])
                    if complete:
                        for discussion in discussions:
                            change(conn, "discussion", discussion["id"], sync_status="current", observed_at=func.now())
        finally:
            client.close()
