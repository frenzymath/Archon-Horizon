"""Repository-scoped claims in Git-backed nodes, bound to their current content."""

import hashlib
import json
from typing import Literal
from uuid import UUID

from pydantic import Field

from .errors import DomainError
from .models import Contract

PROGRESS = ("open", "in_progress", "informal_stated", "proof_sketch", "informal_proved",
            "formally_stated", "formally_proved", "conditionally_proved", "kernel_checked")


class Implementation(Contract):
    status: Literal["open", "in_progress", "informal_stated", "proof_sketch", "informal_proved",
                    "formally_stated", "formally_proved", "conditionally_proved", "kernel_checked"] = "open"
    review: Literal["pending", "statement_accepted", "changes_requested", "accepted"] = "pending"
    node_sha256: str | None = Field(None, pattern=r"^[0-9a-f]{64}$")
    commit_oid: str | None = Field(None, pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    evidence_node_commit_oid: str | None = Field(None, pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    declarations: list[str] = Field(default_factory=list, max_length=1000)
    children: list[str] | None = Field(None, max_length=10000)


def fingerprint(metadata, markdown, children=None):
    # Status, evidence and display-only labels do not change the mathematical contract.
    content = {key: value for key, value in metadata.items()
               if key not in {"implementations", "labels"} and not key.startswith("_horizon_")}
    content["children"] = sorted(children if children is not None else metadata.get("children", []))
    return hashlib.sha256(json.dumps({"metadata": content, "markdown": markdown},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def validate_implementations(metadata, markdown, repository_ids):
    from pydantic import ValidationError
    raw = metadata.get("implementations", {})
    if not isinstance(raw, dict) or len(raw) > 100:
        raise DomainError("invalid_node_implementations", "implementations must map repository UUIDs to progress records", 422)
    parsed = {}
    try:
        for key, value in raw.items():
            repository_id = UUID(key)
            if str(repository_id) != key or repository_id not in repository_ids:
                raise ValueError("Implementation target must be a workspace/library repository in this project")
            item = Implementation.model_validate(value)
            if (item.status != "open" or item.review != "pending") and item.node_sha256 is None:
                raise ValueError("Progress and review claims require node_sha256")
            if item.status in {"formally_proved", "conditionally_proved", "kernel_checked"} and not (item.commit_oid or item.evidence_node_commit_oid):
                raise ValueError("Proof claims require an immutable implementation or evidence-node commit")
            if item.children is not None and (len(set(item.children)) != len(item.children) or any(not key.strip() for key in item.children)):
                raise ValueError("Implementation children must be distinct nonempty node keys")
            parsed[key] = item.model_dump(mode="json", exclude_none=True)
    except (ValueError, TypeError, ValidationError) as error:
        raise DomainError("invalid_node_implementations", str(error), 422) from error
    metadata["implementations"] = parsed
    metadata["_horizon_node_sha256"] = fingerprint(metadata, markdown)
    metadata["_horizon_target_sha256"] = {key: fingerprint(metadata, markdown, item.get("children"))
                                         for key, item in parsed.items()}
    return parsed


def target_context(conn, project_id, repository_id=None, run_id=None):
    from sqlalchemy import select
    from .schema import tables
    repository, run, mission = (tables[name] for name in ("repository", "run", "mission"))
    targets = [dict(row) for row in conn.execute(select(repository.c.id, repository.c.slug, repository.c.purpose)
        .where(repository.c.project_id == project_id, repository.c.archived_at.is_(None),
               repository.c.purpose.in_(["workspace", "library"])).order_by(repository.c.slug)).mappings()]
    if run_id:
        row = conn.execute(select(run.c.phase).join(mission).where(run.c.id == run_id,
            mission.c.project_id == project_id)).mappings().first()
        if not row:
            raise DomainError("scope_mismatch", "Selected run belongs to another project", 422)
    else:
        row = conn.execute(select(run.c.phase).join(mission).where(mission.c.project_id == project_id,
            run.c.status.in_(["active", "paused", "draining"])).order_by(run.c.number.desc()).limit(1)).mappings().first()
    if repository_id is None and row:
        phase = row["phase"]
        repository_id = phase.get("target_repository_id")
        if repository_id is None and phase.get("source_workspace_id"):
            workspace = tables["workspace"]
            repository_id = conn.execute(select(workspace.c.repository_id).where(
                workspace.c.id == UUID(phase["source_workspace_id"]))).scalar_one_or_none()
    if repository_id is None:
        workspaces = [item for item in targets if item["purpose"] == "workspace"]
        repository_id = workspaces[0]["id"] if len(workspaces) == 1 else None
    selected = next((item for item in targets if str(item["id"]) == str(repository_id)), None)
    if repository_id is not None and selected is None:
        raise DomainError("invalid_graph_target", "Graph target must be a workspace/library repository in this project", 422)
    return {"target_repository_id": selected["id"] if selected else None, "target": selected, "targets": targets}


def progress_view(metadata, context):
    labels = list(metadata.get("labels", []))
    target = context.get("target")
    key = str(context.get("target_repository_id"))
    implementations = metadata.get("implementations", {})
    claimed = implementations.get(key)
    digest = metadata.get("_horizon_target_sha256", {}).get(key, metadata.get("_horizon_node_sha256"))
    # Old unscoped labels describe the workspace only, never a new library.
    scoped = bool(implementations) or bool(target and target["purpose"] == "library")
    if not scoped:
        return {"labels": labels, "implementation": None, "node_sha256": digest}
    stale = bool(claimed and claimed.get("node_sha256") and claimed["node_sha256"] != digest)
    status = "stale" if stale else (claimed or {}).get("status", "open")
    return {"labels": [label for label in labels if label not in PROGRESS] + [status],
            "node_sha256": digest,
            "implementation": {"repository_id": context.get("target_repository_id"), "status": status,
                "review": "pending" if stale else (claimed or {}).get("review", "pending"),
                "stale": stale, "recorded": claimed}}


def main():
    import argparse
    from pathlib import Path
    from .documents import parse_document
    parser = argparse.ArgumentParser(description="Compute the content digest for a node implementation claim")
    parser.add_argument("file", type=Path, nargs="?")
    parser.add_argument("--repository-id", type=UUID)
    parser.add_argument("--guide", action="store_true", help="Print the installed repository progress contract")
    args = parser.parse_args()
    if args.guide:
        print((Path(__file__).parent / "skills/operations/horizon-graph/references/repository-progress.md").read_text())
        return
    if args.file is None or args.repository_id is None:
        parser.error("file and --repository-id are required unless --guide is used")
    metadata, markdown = parse_document(args.file.read_text())
    implementation = metadata.get("implementations", {}).get(str(args.repository_id), {})
    print(fingerprint(metadata, markdown, implementation.get("children")))


if __name__ == "__main__":
    main()
