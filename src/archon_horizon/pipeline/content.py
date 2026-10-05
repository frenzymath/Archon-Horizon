"""Authenticated uploads of bounded, immutable evidence blobs."""

from __future__ import annotations

import base64
import binascii
from uuid import UUID

from pydantic import Field, JsonValue, model_validator
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from .auth import live_execution, require_project
from .errors import DomainError
from .models import Contract
from .records import create, emit
from .schema import tables


class BlobUpload(Contract):
    project_id: UUID
    json_content: JsonValue | None = None
    content_base64: str | None = None
    media_type: str = Field(default="application/octet-stream", pattern=r"^[a-zA-Z0-9.+-]+/[a-zA-Z0-9.+-]+$")

    @model_validator(mode="after")
    def one_content(self):
        if (self.json_content is None) == (self.content_base64 is None):
            raise ValueError("provide json_content or content_base64")
        return self


def upload(conn, actor, data: BlobUpload, service):
    require_project(conn, actor, data.project_id, "worker")
    from .storage import pressure
    if pressure(service.config.state_root, service.config.storage)["status"] == "pause":
        raise DomainError("storage_pressure", "Evidence upload paused until storage is repaired", 503)
    execution = live_execution(conn, actor) if actor.kind == "agent" else None
    if data.content_base64 is not None:
        try:
            content = base64.b64decode(data.content_base64, validate=True)
        except (binascii.Error, ValueError):
            raise DomainError("invalid_blob", "Blob content is not valid base64", 422) from None
        identity = service.store.put(content, data.media_type)
    else:
        identity = service.store.put_json(data.json_content)
    table = tables["artifact"]
    existing = conn.execute(select(table).where(table.c.project_id == data.project_id, table.c.kind == "blob",
        table.c.content["sha256"].astext == identity["sha256"])).mappings().first()
    row = dict(existing) if existing else create(conn, "artifact", project_id=data.project_id, kind="blob",
        content=identity, created_by_execution_id=execution["id"] if execution else None)
    if not existing:
        create(conn, "artifact_location", artifact_id=row["id"], locator=str(service.store.path(identity["sha256"])),
               verified_at=func.now())
    if execution:
        conn.execute(insert(tables["assignment_artifact"]).values(assignment_id=execution["assignment_id"],
            artifact_id=row["id"]).on_conflict_do_nothing())
    emit(conn, actor.id, data.project_id, "artifact", row, ["content"])
    return row
