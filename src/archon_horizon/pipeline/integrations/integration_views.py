"""Human-facing integration links are independent of connector transport URLs."""

from urllib.parse import quote, urlsplit

from sqlalchemy import select

from ..auth import require_project
from ..persistence.schema import tables


def public_url(integration, config=None):
    configured = config.integration_public_urls.get(integration["id"]) if config else None
    if configured:
        return configured.rstrip("/")
    endpoint = integration["endpoint"].rstrip("/")
    if urlsplit(endpoint).hostname in {"localhost", "127.0.0.1", "::1"}:
        return None
    return endpoint


def repository_url(repository, integration, config=None):
    base = public_url(integration, config)
    path = repository.get("remote_path")
    return base + "/" + quote(path, safe="/") if base and path else None


def project_integrations(conn, actor, project_id, config):
    require_project(conn, actor, project_id)
    repository, discussion, integration = (tables[name] for name in ("repository", "discussion", "integration"))
    repositories = list(conn.execute(select(repository).where(repository.c.project_id == project_id,
        repository.c.archived_at.is_(None)).order_by(repository.c.slug)).mappings())
    discussions = list(conn.execute(select(discussion).where(discussion.c.project_id == project_id)
        .order_by(discussion.c.topic).limit(100)).mappings())
    ids = {row["integration_id"] for row in [*repositories, *discussions]}
    result = []
    for row in conn.execute(select(integration).where(integration.c.id.in_(ids)).order_by(integration.c.kind)).mappings():
        base = public_url(row, config)
        result.append({"id": row["id"], "kind": row["kind"], "public_url": base,
            "enabled": row["enabled"], "browser_session": row["id"] in config.integration_browser_auth, "repositories": [
                {"id": repo["id"], "name": repo["slug"], "purpose": repo["purpose"],
                 "url": repository_url(repo, row, config)} for repo in repositories if repo["integration_id"] == row["id"]],
            "discussions": [{"id": topic["id"], "topic": topic["topic"],
                "url": (base + "/#narrow/stream/" + quote(topic["channel_remote_id"], safe="") + "/topic/" +
                        quote(topic["topic"], safe="")) if base else None}
                for topic in discussions if topic["integration_id"] == row["id"]]})
    return {"items": result}
