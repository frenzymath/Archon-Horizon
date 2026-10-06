from __future__ import annotations

import json
import math
import time
import uuid
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any


OPERATION_KINDS = frozenset({
    "activity", "execution_finished", "publication_discovered", "publication_verified", "publication_failed", "provider_observed",
    "workspace_prepared",
})
EXECUTION_OUTCOMES = frozenset({"succeeded", "failed", "cancelled", "lost", "yielded"})


def validate_tool_environment(values: dict[str, str]) -> dict[str, str]:
    path_lists = {"PATH", "LEAN_PATH", "LEAN_SRC_PATH"}
    directories = {"ELAN_HOME", "LAKE_HOME", "XDG_CACHE_HOME"}
    allowed = path_lists | directories | {"LANG", "LC_ALL", "TZ"}
    for key, value in values.items():
        if key not in allowed:
            raise ValueError("environment supports only explicit tool paths and locale settings")
        if not isinstance(value, str) or not value or len(value) > 16384 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("environment values must be bounded nonempty single-line strings")
        if key in path_lists | directories:
            for entry in value.split(":") if key in path_lists else [value]:
                path = PurePosixPath(entry)
                if not path.is_absolute() or ".." in path.parts or "$" in entry:
                    raise ValueError("tool paths must be explicit absolute paths without empty entries or variable expansion")
    return values


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def require_identifier(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 200:
        raise ValueError("identifier must be nonempty and at most 200 characters")
    if any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in value):
        raise ValueError("identifier contains unsafe characters")
    return value


@dataclass(frozen=True)
class Operation:
    operation_id: str
    execution_id: str
    epoch: int
    kind: str
    payload: dict[str, Any]
    occurred_at: float

    def __post_init__(self) -> None:
        require_identifier(self.operation_id)
        require_identifier(self.execution_id)
        if isinstance(self.epoch, bool) or not isinstance(self.epoch, int) or self.epoch < 1:
            raise ValueError("epoch must be a positive integer")
        if self.kind not in OPERATION_KINDS:
            raise ValueError("unknown worker operation kind")
        if not isinstance(self.payload, dict):
            raise ValueError("operation payload must be an object")
        if not math.isfinite(self.occurred_at) or self.occurred_at <= 0:
            raise ValueError("occurred_at must be a positive Unix timestamp")
        if self.kind == "execution_finished" and self.payload.get("status") not in EXECUTION_OUTCOMES:
            raise ValueError("invalid execution outcome")
        if self.kind in {"publication_discovered", "publication_verified", "publication_failed"}:
            if not all(self.payload.get(k) for k in ("repository_id", "commit_oid", "recovery_ref")):
                raise ValueError("publication discovery requires repository, commit, and recovery ref")
        if self.kind == "publication_verified" and not all(self.payload.get(k) for k in ("remote", "remote_ref")):
            raise ValueError("publication verification requires configured remote and verified ref")
        canonical_json(self.payload)

    @classmethod
    def create(cls, execution_id: str, epoch: int, kind: str, payload: dict[str, Any],
               *, operation_id: str | None = None, occurred_at: float | None = None) -> Operation:
        return cls(operation_id or str(uuid.uuid4()), execution_id, epoch, kind,
                   payload, time.time() if occurred_at is None else occurred_at)

    def as_dict(self) -> dict[str, Any]:
        return {"operation_id": self.operation_id, "execution_id": self.execution_id,
                "epoch": self.epoch, "kind": self.kind, "payload": self.payload,
                "occurred_at": self.occurred_at}


@dataclass(frozen=True)
class ClaimedOperation:
    operation: Operation
    claim_token: str
    attempts: int


@dataclass(frozen=True)
class ExecutionGrant:
    execution_id: str
    assignment_id: str
    epoch: int
    lease_seconds: float
    harness_id: str
    workspace_id: str
    workspace_path: str
    repository_id: str
    goal: str
    execution_token: str | None = field(default=None, repr=False)
    provider_thread_id: str | None = None
    provider_thread_record_id: str | None = None
    mission_revision_id: str | None = None
    mission_revision_number: int = 1
    run_revision: int = 1
    roadmap_snapshot_id: str | None = None
    harness_configuration: dict[str, Any] | None = None
    max_offline_replay_seconds: int = 604800
    max_parallel_subagents: int = 0
    role: str = "worker"
    functions: tuple[str, ...] = ()
    skill_bundle_sha256: str | None = None
    skill_bundle: dict[str, Any] | None = None
    goal_artifact_id: str | None = None
    sandbox_manifest: dict[str, Any] | None = None
    workspace_preparation: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        for value in (self.execution_id, self.assignment_id, self.harness_id, self.repository_id, self.workspace_id):
            require_identifier(value)
        if self.epoch < 1 or not math.isfinite(self.lease_seconds) or self.lease_seconds < 0:
            raise ValueError("invalid execution lease")
        if self.max_offline_replay_seconds < 1:
            raise ValueError("offline replay horizon must be positive")
        if self.max_parallel_subagents < 0:
            raise ValueError("native subagent capacity must be nonnegative")
        if not self.workspace_path.startswith("/") or (not self.goal.strip() and not self.goal_artifact_id):
            raise ValueError("grant requires an absolute workspace and nonempty goal")


class FencedExecution(RuntimeError):
    pass


class JournalFull(RuntimeError):
    """Producer must checkpoint/pause; existing records remain protected."""
