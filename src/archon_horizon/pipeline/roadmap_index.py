"""Explicit indexing of committed roadmap Markdown; never edits source repositories."""

from pathlib import Path, PurePosixPath
import subprocess

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from .documents import parse_document
from .auth import require_project
from .catalog import CatalogUpdate, create_catalog, update_catalog
from .conditions import reject_cycle
from .errors import DomainError
from .records import get
from .schema import tables


def read_git_snapshot(repository: Path, revision: str = "HEAD"):
    """Read regular Markdown blobs at one commit, with explicit memory bounds."""
    def git(*args):
        return subprocess.run(["git", "-C", str(repository), *args], check=True,
            capture_output=True, timeout=30).stdout

    commit = git("rev-parse", "--verify", "--end-of-options", revision + "^{commit}").decode().strip()
    entries = git("ls-tree", "-rlz", commit).split(b"\0")
    sources = {}
    total = 0
    for entry in entries:
        if not entry:
            continue
        info, encoded_path = entry.split(b"\t", 1)
        mode, kind, oid, size = info.split()
        path = encoded_path.decode("utf-8")
        selected = (path.startswith(("nodes/", "objectives/", "milestones/")) and path.endswith((".md", ".lean"))
                    or path.endswith('.lean') or path in {"lean-toolchain", "lakefile.toml", "lake-manifest.json"})
        if not selected:
            continue
        if mode not in {b"100644", b"100755"} or kind != b"blob":
            raise DomainError("invalid_roadmap_source", "Roadmap sources must be regular Git blobs", 422)
        total += int(size)
        if int(size) > 2 * 1024**2 or total > 32 * 1024**2 or len(sources) >= 10000:
            raise DomainError("roadmap_too_large", "Roadmap exceeds the bounded source index size", 422)
        sources[path] = git("cat-file", "blob", oid.decode()).decode("utf-8")
    return commit, sources


def index_snapshot(conn, actor, repository_id, commit, sources):
    from .milestone_sources import source_manifest
    from .graph_progress import validate_implementations
    repository = get(conn, "repository", repository_id, lock=True)
    require_project(conn, actor, repository["project_id"], "maintainer")
    if repository["purpose"] != "knowledge":
        raise DomainError("invalid_roadmap_repository", "Roadmap source must be a knowledge repository", 422)
    try:
        source_manifest(sources)
    except ValueError as error:
        raise DomainError("invalid_milestones", str(error), 422) from error
    target_table = tables["repository"]
    target_ids = set(conn.execute(select(target_table.c.id).where(
        target_table.c.project_id == repository["project_id"], target_table.c.purpose.in_(["workspace", "library"]),
        target_table.c.archived_at.is_(None))).scalars())
    parsed, keys, additional = {}, {}, {}
    for path, text in sources.items():
        parts = PurePosixPath(path).parts
        if parts and '..' not in parts and not PurePosixPath(path).is_absolute() and (
                path.endswith('.lean') or path in {'lean-toolchain', 'lakefile.toml', 'lake-manifest.json'}
                or path.startswith('milestones/') and path.endswith('.md')):
            additional[path] = {"metadata": {}, "markdown": text}
            continue
        if not parts or parts[0] not in {"nodes", "objectives"} or ".." in parts or not path.endswith(".md"):
            raise DomainError("invalid_roadmap_path", "Roadmap paths must be Markdown under nodes/ or objectives/", 422)
        metadata, markdown = parse_document(text)
        key = metadata.get("label") or metadata.get("id") or PurePosixPath(path).stem
        title = metadata.get("title") or key
        if not isinstance(key, str) or not isinstance(title, str):
            raise DomainError("invalid_roadmap_metadata", "Node identity and title must be strings", 422)
        if parts[0] == "nodes":
            if key in keys:
                raise DomainError("duplicate_roadmap_key", f"Duplicate roadmap node key: {key}", 422)
            keys[key] = path
        for field in ("labels", "children"):
            if not isinstance(metadata.get(field, []), list) or not all(isinstance(item, str) for item in metadata.get(field, [])):
                raise DomainError("invalid_roadmap_metadata", f"{field} must be a list of strings", 422)
        if parts[0] == "nodes":
            validate_implementations(metadata, markdown, target_ids)
        parsed[path] = {"key": key, "title": title, "metadata": metadata, "markdown": markdown}
    edges = {key: set(parsed[path]["metadata"].get("children", [])) for key, path in keys.items()}
    missing = sorted({parent for parents in edges.values() for parent in parents if parent not in keys})
    if missing:
        raise DomainError("missing_roadmap_dependencies", "Referenced roadmap nodes are absent", 422, missing=missing)
    reject_cycle(edges)
    for target in {target for path in keys.values() for target in parsed[path]["metadata"]["implementations"]}:
        target_edges = {key: set(parsed[path]["metadata"]["implementations"].get(target, {}).get("children",
            parsed[path]["metadata"].get("children", []))) for key, path in keys.items()}
        missing = sorted({child for children in target_edges.values() for child in children if child not in keys})
        if missing:
            raise DomainError("missing_roadmap_dependencies", "Target dependencies are absent", 422, missing=missing)
        reject_cycle(target_edges)
    rows = {}
    for path, source in parsed.items():
        kind = "node" if path.startswith("nodes/") else "document"
        table = tables[kind]
        old = conn.execute(select(table).where(table.c.source_repository_id == repository_id,
            table.c.source_path == path)).mappings().first()
        values = {"title": source["title"], "source_commit_oid": commit}
        if old:
            changes = {key: value for key, value in values.items() if old[key] != value}
            rows[path] = update_catalog(conn, actor, kind, old["id"], CatalogUpdate(
                expected_revision=old["revision"], changes=changes)) if changes else dict(old)
        else:
            rows[path] = create_catalog(conn, actor, kind, {**values, "project_id": repository["project_id"],
                "source_repository_id": repository_id, "source_path": path,
                **({"kind": "roadmap"} if kind == "document" else {})})
    dependency = tables["node_dependency"]
    for key, parents in edges.items():
        row = rows[keys[key]]
        ids = {rows[keys[parent]]["id"] for parent in parents}
        existing = set(conn.execute(select(dependency.c.parent_node_id).where(dependency.c.child_node_id == row["id"])).scalars())
        if ids != existing:
            update_catalog(conn, actor, "node", row["id"], CatalogUpdate(expected_revision=row["revision"],
                changes={"parent_node_ids": sorted(ids, key=str)}))
    projection = tables["source_projection"]
    conn.execute(delete(projection).where(projection.c.repository_id == repository_id))
    projected = {**parsed, **additional}
    if projected:
        conn.execute(insert(projection), [{"repository_id": repository_id, "source_path": path,
            "source_commit_oid": commit, "metadata": source["metadata"], "markdown": source["markdown"]}
            for path, source in projected.items()])
    return {"repository_id": repository_id, "source_commit_oid": commit,
        "nodes": len(keys), "documents": len(parsed) - len(keys), "dependencies": sum(map(len, edges.values()))}


def main():
    import argparse
    import json
    from uuid import UUID

    from .auth import Actor
    from .config import load_config
    from .database import Database
    from .records import json_value, transaction_lock

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--operator", required=True)
    parser.add_argument("--repository-id", type=UUID, required=True)
    parser.add_argument("--git-directory", type=Path, required=True)
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--skip-unchanged", action="store_true")
    args = parser.parse_args()
    config = load_config(args.config)
    with Database(config.database_url.get_secret_value()) as database:
        database.check_revision()
        if args.skip_unchanged:
            commit = subprocess.run(["git", "-C", str(args.git_directory), "rev-parse", "--verify",
                "--end-of-options", args.revision + "^{commit}"], check=True, capture_output=True, timeout=10).stdout.decode().strip()
            projection = tables["source_projection"]
            with database.engine.connect() as conn:
                indexed = set(conn.execute(select(projection.c.source_commit_oid).where(
                    projection.c.repository_id == args.repository_id).distinct()).scalars())
            if indexed == {commit}:
                print(json.dumps({"unchanged": True, "source_commit_oid": commit}))
                return
        commit, sources = read_git_snapshot(args.git_directory, args.revision)
        with database.engine.connect() as conn:
            transaction = conn.begin()
            try:
                transaction_lock(conn)
                principal = tables["principal"]
                operator = conn.execute(select(principal).where(principal.c.username == args.operator,
                    principal.c.kind == "human")).mappings().one()
                actor = Actor(operator["id"], "human", {"username": args.operator}, "api_key")
                result = index_snapshot(conn, actor, args.repository_id, commit, sources)
                if args.apply:
                    transaction.commit()
                else:
                    transaction.rollback()
                print(json.dumps({"applied": args.apply, **json_value(result)}, indent=2))
            finally:
                if transaction.is_active:
                    transaction.rollback()


if __name__ == "__main__":
    main()
