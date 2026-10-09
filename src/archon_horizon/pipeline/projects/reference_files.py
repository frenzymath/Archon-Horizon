"""Project-authorized source documents backed by immutable shared artifacts.

Filenames are display metadata, never storage paths. Uploaded archives are kept
as bytes; the server does not extract, compile, or execute source documents.
"""
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from ..auth import live_execution, require_project
from ..errors import DomainError
from ..models import Contract
from ..persistence.records import create, emit, get
from ..persistence.schema import tables


class ReferenceFileUpload(Contract):
    filename: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=4000)
    source_url: str | None = Field(default=None, max_length=4000)

    @field_validator("filename")
    @classmethod
    def safe_filename(cls, value):
        if value in {".", ".."} or any(c in value for c in '/\\') or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("filename must be a single name without path separators or control characters")
        return value

    @field_validator("source_url")
    @classmethod
    def source_link(cls, value):
        if value is not None:
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("source URL must be HTTP(S) without credentials")
        return value


def media_type(filename: str, path: Path) -> str:
    """Only PDFs and validated UTF-8 text are previewable on the dashboard origin."""
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        with path.open("rb") as source:
            if source.read(5) != b"%PDF-":
                raise DomainError("invalid_reference_file", "A PDF file must have a PDF header", 422)
        return "application/pdf"
    if suffix in {".tex", ".txt", ".md", ".bib", ".csv", ".json", ".sty", ".cls"}:
        import codecs
        decoder = codecs.getincrementaldecoder("utf-8")()
        try:
            with path.open("rb") as source:
                while chunk := source.read(65536):
                    if b"\0" in chunk:
                        raise UnicodeError("NUL in text")
                    decoder.decode(chunk)
                decoder.decode(b"", final=True)
        except UnicodeError:
            # Historical TeX can use legacy encodings. Preserve the original
            # bytes for download instead of guessing an encoding for previews.
            return "application/octet-stream"
        return "text/plain"
    return "application/octet-stream"


def present(row, artifact):
    content = artifact["content"]
    return {**row, "sha256": content["sha256"], "size_bytes": content["size_bytes"],
            "media_type": content["media_type"],
            "previewable": content["media_type"] in {"application/pdf", "text/plain"},
            "content_url": f"/api/v3/references/{row['reference_id']}/files/{row['id']}/content"}


def list_files(conn, actor, reference_id, cursor=None, limit=50):
    from ..dashboard.readmodels import page
    reference = get(conn, "reference", reference_id)
    require_project(conn, actor, reference["project_id"])
    table = tables["reference_file"]
    result = page(conn, select(table).where(table.c.reference_id == reference_id, table.c.archived_at.is_(None)), table, cursor, limit)
    artifacts = {row["id"]: dict(row) for row in conn.execute(select(tables["artifact"]).where(
        tables["artifact"].c.id.in_([file["artifact_id"] for file in result["items"]]))).mappings()}
    result["items"] = [present(row, artifacts[row["artifact_id"]]) for row in result["items"]]
    return result


def read_file(conn, actor, reference_id, file_id):
    reference = get(conn, "reference", reference_id)
    require_project(conn, actor, reference["project_id"])
    row = get(conn, "reference_file", file_id)
    if row["reference_id"] != reference_id:
        raise DomainError("not_found", "File is not attached to this reference", 404)
    if row["archived_at"] is not None:
        raise DomainError("reference_file_archived", "This reference file is archived", 410)
    artifact = get(conn, "artifact", row["artifact_id"])
    if artifact["project_id"] != reference["project_id"] or artifact["kind"] != "blob":
        raise DomainError("reference_file_invalid", "Reference file has inconsistent storage", 503)
    return reference, row, artifact


def attach(conn, actor, reference_id: UUID, data: ReferenceFileUpload, identity: dict, service):
    reference = get(conn, "reference", reference_id)
    require_project(conn, actor, reference["project_id"], "worker")
    execution = live_execution(conn, actor) if actor.kind == "agent" else None
    table = tables["artifact"]
    existing = conn.execute(select(table).where(table.c.project_id == reference["project_id"], table.c.kind == "blob",
        table.c.content["sha256"].astext == identity["sha256"], table.c.content["media_type"].astext == identity["media_type"])).mappings().first()
    artifact = dict(existing) if existing else create(conn, "artifact", project_id=reference["project_id"], kind="blob",
        content=identity, created_by_execution_id=execution["id"] if execution else None)
    if not existing:
        create(conn, "artifact_location", artifact_id=artifact["id"], locator=str(service.store.path(identity["sha256"])), verified_at=func.now())
    if execution:
        conn.execute(insert(tables["assignment_artifact"]).values(assignment_id=execution["assignment_id"], artifact_id=artifact["id"]).on_conflict_do_nothing())
    files = tables["reference_file"]
    previous = conn.execute(select(files).where(files.c.reference_id == reference_id, files.c.artifact_id == artifact["id"],
        files.c.filename == data.filename)).mappings().first()
    if previous:
        # Identical bytes/name converge even when clients retry with another key.
        # Changed provenance requires an explicit new file name, never a silent edit.
        if previous["archived_at"] is not None or previous["description"] != data.description or previous["source_url"] != data.source_url:
            raise DomainError("reference_file_exists", "This file already exists with different metadata or was archived; use a distinct filename", 409)
        return present(dict(previous), artifact)
    row = create(conn, "reference_file", reference_id=reference_id, artifact_id=artifact["id"], **data.model_dump())
    emit(conn, actor.id, reference["project_id"], "reference", reference, ["files"])
    return present(row, artifact)


def archive(conn, actor, reference_id, file_id):
    from ..persistence.records import change
    reference, row, _ = read_file(conn, actor, reference_id, file_id)
    require_project(conn, actor, reference["project_id"], "worker")
    row = change(conn, "reference_file", row["id"], row["revision"], archived_at=datetime.now(timezone.utc))
    emit(conn, actor.id, reference["project_id"], "reference", reference, ["files"])
    return row
