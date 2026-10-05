from __future__ import annotations

import json
import time
from datetime import timezone
from email.utils import parsedate_to_datetime
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import httpx
from tenacity import wait_random_exponential

from .contracts import ExecutionGrant, FencedExecution
from .journal import DurableJournal


class WorkerTransport:
    """One bounded HTTP attempt per call; durable retry timing lives in SQLite."""

    def __init__(self, base_url: str, token: str, *, client: httpx.Client | None = None,
                 max_attempts: int = 20, initial_delay: float = 1, max_delay: float = 300) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("invalid API base URL")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1", "testserver"}:
            raise ValueError("remote worker API connections require HTTPS")
        if not token or max_attempts < 1 or initial_delay <= 0 or max_delay < initial_delay:
            raise ValueError("invalid transport configuration")
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._owned_client = client is None
        self.client = client or httpx.Client(timeout=httpx.Timeout(10, connect=5),
                                             limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
                                             follow_redirects=False, trust_env=False)
        self.max_attempts = max_attempts
        self._wait = wait_random_exponential(multiplier=initial_delay, max=max_delay)

    def _post(self, path: str, body: dict[str, Any], *, key: str | None = None) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._token}"}
        if key:
            headers["Idempotency-Key"] = key
        return self.client.post(self.base_url + path, json=body, headers=headers,
                                timeout=httpx.Timeout(10, connect=5), follow_redirects=False)

    def claim(self, host_id: str, harness_ids: list[str], *, request_id: str | None = None) -> ExecutionGrant | None:
        response = self._post("/api/v3/worker/claim", {"host_id": host_id, "harness_ids": harness_ids}, key=request_id)
        response.raise_for_status()
        execution = response.json()["execution"]
        return ExecutionGrant(**execution) if execution is not None else None

    def goal(self, grant: ExecutionGrant) -> str:
        content = self._get_json(f"/api/v3/worker/executions/{grant.execution_id}/artifacts/{grant.goal_artifact_id}/content")
        if not isinstance(content.get("goal"), str) or not content["goal"].strip():
            raise ValueError("goal artifact must contain a nonempty goal")
        return content["goal"]

    def _get_json(self, path: str, *, token: str | None = None) -> dict[str, Any]:
        data = bytearray()
        with self.client.stream("GET", self.base_url + path,
                                headers={"Authorization": f"Bearer {token or self._token}"},
                                timeout=httpx.Timeout(10, connect=5), follow_redirects=False) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > 16 * 1024**2:
                    raise ValueError("worker artifact exceeds response limit")
        content = json.loads(data)
        if not isinstance(content, dict):
            raise ValueError("worker artifact must be a JSON object")
        return content

    def skill_bundle(self, execution_id: str) -> dict[str, Any]:
        return self._get_json(f"/api/v3/worker/executions/{execution_id}/skill-bundle")

    def reviewer_accounts(self, grant: ExecutionGrant) -> dict[str, Any]:
        if not grant.execution_token:
            return {"execution_id": grant.execution_id, "accounts": []}
        content = self._get_json(f"/api/v3/executions/{grant.execution_id}/reviewer-accounts",
                                 token=grant.execution_token)
        if (content.get("execution_id") != grant.execution_id or not isinstance(content.get("accounts"), list)
                or any(not isinstance(account, dict) or not isinstance(account.get("token"), str)
                       or not account["token"] for account in content["accounts"])):
            raise ValueError("invalid scoped reviewer credentials response")
        return content

    def heartbeat(self, execution_id: str, epoch: int, *, provider_thread_id: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"epoch": epoch}
        if provider_thread_id is not None:
            body["provider_thread_id"] = provider_thread_id
        response = self._post(f"/api/v3/worker/executions/{execution_id}/heartbeat", body)
        if response.status_code in {401, 403, 404, 409, 410}:
            raise FencedExecution("execution renewal refused by control plane")
        response.raise_for_status()
        return response.json()

    def host_heartbeat(self, host_id: str, health: dict[str, Any]) -> None:
        response = self._post(f"/api/v3/worker/hosts/{host_id}/heartbeat", {"health": health})
        response.raise_for_status()

    @staticmethod
    def _retry_after(response: httpx.Response, now: float) -> float:
        value = response.headers.get("Retry-After", "")
        try:
            return max(0, float(value))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return max(0, parsed.timestamp() - now)
            except (ValueError, TypeError, OverflowError):
                return 0

    def replay_one(self, journal: DurableJournal, *, now: float | None = None) -> str | None:
        now = time.time() if now is None else now
        claimed = journal.claim(now=now)
        if claimed is None:
            return None
        retry_after = 0.0
        try:
            response = self._post("/api/v3/worker/operations", claimed.operation.as_dict(),
                                  key=claimed.operation.operation_id)
        except httpx.TransportError:
            error = "transport_unavailable"
        else:
            status = response.status_code
            if 200 <= status < 300:
                journal.settle(claimed, "acknowledged", now=now)
                return "acknowledged"
            if status in {409, 410}:
                journal.settle(claimed, "recovery", error="fenced_or_conflicting_operation")
                return "recovery"
            if status not in {408, 425, 429} and status < 500:
                journal.settle(claimed, "blocked", error=f"http_{status}")
                return "blocked"
            error = f"http_{status}"
            retry_after = self._retry_after(response, now)
        durable_evidence = claimed.operation.kind in {"publication_discovered", "publication_verified", "publication_failed", "execution_finished", "workspace_prepared"}
        if claimed.attempts >= self.max_attempts and not durable_evidence:
            journal.settle(claimed, "recovery", error="retry_budget_exhausted:" + error)
            return "recovery"
        delay = max(retry_after, self._wait(SimpleNamespace(attempt_number=min(claimed.attempts, 32))))
        journal.settle(claimed, "pending", retry_at=now + delay, error=error)
        return "pending"

    def close(self) -> None:
        if self._owned_client:
            self.client.close()
