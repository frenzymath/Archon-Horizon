"""Bibliographic export from the project catalog using a structured BibTeX writer."""

from __future__ import annotations

from uuid import UUID

from pybtex.database import BibliographyData, Entry, Person
from pybtex.exceptions import PybtexError
from sqlalchemy import func, literal_column, or_, select

from .auth import require_project
from .models import Contract, ObjectRef, Text
from .errors import DomainError
from .records import create, emit, get, object_ref, same_project
from .reference_identifiers import NORMALIZERS, identifier_key
from .schema import tables


def identifier_expression(table, kind):
    if kind not in NORMALIZERS:
        raise ValueError("unknown reference identifier kind")
    # Fixed expression operands let PostgreSQL match expression indexes even
    # after psycopg switches repeated lookups to a generic prepared plan.
    value = table.c.identifiers.op("->>")(literal_column(f"'{kind}'"))
    if kind == "arxiv":
        value = func.regexp_replace(func.regexp_replace(value, literal_column("'v[1-9][0-9]*$'"),
            literal_column("''")), literal_column(r"'\.[A-Z]{2}/'"), literal_column("'/'"))
    return value


def reject_duplicate(conn, values, identifier=None):
    table = tables["reference"]
    matches = [table.c.cite_key == values["cite_key"]]
    matches.extend(identifier_expression(table, kind) == identifier_key(kind, value)
                   for kind, value in values["identifiers"].items() if value is not None)
    query = select(table.c.id, table.c.cite_key).where(table.c.project_id == values["project_id"], or_(*matches))
    if identifier:
        query = query.where(table.c.id != identifier)
    existing = [dict(row) for row in conn.execute(query).mappings()]
    if existing:
        raise DomainError("reference_exists" if len(existing) == 1 else "reference_identifier_conflict",
            "Reference identifiers or citation key already belong to the project catalog; inspect existing entries before updating",
            409, existing=[{"id": str(row["id"]), "cite_key": row["cite_key"]} for row in existing])


class ReferenceUsage(Contract):
    reference_id: UUID
    subject: ObjectRef
    locator: Text | None = None


def record_usage(conn, actor, data: ReferenceUsage):
    reference = get(conn, "reference", data.reference_id)
    require_project(conn, actor, reference["project_id"], "worker")
    subject = data.subject.model_dump(mode="json")
    path = subject.get("path")
    kind = "repository" if path else subject["kind"]
    identifier = subject.get("repository_id", subject.get("id"))
    same_project(conn, kind, identifier, reference["project_id"])
    ref = object_ref(conn, subject["kind"], identifier, path)
    row = create(conn, "reference_usage", reference_id=reference["id"], subject_id=ref,
                 locator=data.locator, cited_as=reference["cite_key"])
    emit(conn, actor.id, reference["project_id"], "reference", reference, ["usage"])
    return {"usage": row, "reference": reference, "bibtex": bibtex([reference])}


def bibtex(references: list[dict]) -> str:
    try:
        return _bibtex(references)
    except PybtexError as error:
        raise DomainError("invalid_reference_metadata", "Reference metadata cannot be exported as BibTeX; "
                          "check author names and balanced TeX braces", 422) from error


def _bibtex(references: list[dict]) -> str:
    kinds = {"article": "article", "book": "book", "inproceedings": "inproceedings",
             "thesis": "phdthesis", "report": "techreport", "webpage": "misc", "dataset": "misc", "other": "misc"}
    entries = {}
    for reference in references:
        fields = {"title": reference["title"]}
        if reference["issued_year"] is not None:
            fields["year"] = str(reference["issued_year"])
        if reference["venue"]:
            venue_fields = {"article": "journal", "inproceedings": "booktitle", "book": "publisher",
                            "thesis": "school", "report": "institution"}
            fields[venue_fields.get(reference["kind"], "howpublished")] = reference["venue"]
        identifiers = reference["identifiers"]
        for name in ("doi", "isbn", "pmid"):
            if identifiers.get(name):
                fields[name] = identifiers[name]
        if identifiers.get("arxiv"):
            fields.update(archivePrefix="arXiv", eprint=identifiers["arxiv"])
        if reference["urls"]:
            fields["url"] = reference["urls"][0]
        entries[reference["cite_key"]] = Entry(kinds[reference["kind"]], fields=fields,
            persons={"author": [Person(author) for author in reference["authors"]]})
    return BibliographyData(entries).to_string("bibtex")
