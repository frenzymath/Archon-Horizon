from uuid import uuid4

import pytest
from pydantic import ValidationError
from pybtex.database import parse_string
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from archon_horizon.pipeline.projects.catalog import CatalogUpdate, create_catalog, update_catalog
from archon_horizon.pipeline.errors import DomainError
from archon_horizon.pipeline.models import ReferenceCreate, ReferenceIdentifiers
from archon_horizon.pipeline.persistence.records import create
from archon_horizon.pipeline.projects.reference_identifiers import arxiv_key, normalize_identifier
from archon_horizon.pipeline.projects.references import ReferenceUsage, bibtex, record_usage
from archon_horizon.pipeline.persistence.schema import tables
from test_pipeline_service import service_database, world  # noqa: F401


@pytest.mark.parametrize("kind,value,canonical", [
    ("doi", " DOI: https://doi.org/10.1000/ABC%2FDef?source=copy ", "10.1000/abc/def"),
    ("doi", "https://doi.org/10.1000/ABC/", "10.1000/abc/"),
    ("doi", "10.1002/(SICI)1099-0844(199912)17:4<290::AID-CBF849>3.0.CO;2-P", "10.1002/(sici)1099-0844(199912)17:4<290::aid-cbf849>3.0.co;2-p"),
    ("arxiv", "https://arxiv.org/pdf/2301.01234v2.pdf", "2301.01234v2"),
    ("arxiv", "ARXIV:Math.gt/0309136V2", "math.GT/0309136v2"),
    ("isbn", "ISBN-10: 0-201-53082-1", "9780201530827"),
    ("isbn", "978-0-201-53082-7", "9780201530827"),
    ("pmid", "https://pubmed.ncbi.nlm.nih.gov/00012345/", "12345"),
])
def test_identifier_normalization_is_canonical(kind, value, canonical):
    assert normalize_identifier(kind, value) == canonical
    assert normalize_identifier(kind, canonical) == canonical
    assert ReferenceIdentifiers.model_validate({kind: value}).model_dump() == {kind: canonical}


@pytest.mark.parametrize("kind,value", [
    ("doi", "https://untrusted.invalid/10.1000/test"), ("doi", "10.1000/a b"),
    ("arxiv", "2301.1234"), ("arxiv", "0701.1234"), ("arxiv", "2301.12345v0"),
    ("isbn", "9780201530828"), ("pmid", "0"), ("pmid", "not a number"),
])
def test_malformed_identifiers_are_rejected(kind, value):
    with pytest.raises(ValidationError):
        ReferenceIdentifiers.model_validate({kind: value})


def data(world, **changes):
    return {"project_id": world.project["id"], "cite_key": "perelman2002", "kind": "article", "title": "Entropy formula",
            "authors": ["Perelman, Grigori"], "issued_year": 2002, "identifiers": {"arxiv": "math.DG/0211159v1"}, **changes}


def test_catalog_duplicate_detection_is_project_scoped_and_preserves_arxiv_version(world):
    first = create_catalog(world.conn, world.actor, "reference", data(world))
    with pytest.raises(DomainError) as error:
        create_catalog(world.conn, world.actor, "reference", data(world, cite_key="duplicate", identifiers={"arxiv": "math/0211159v2"}))
    assert error.value.code == "reference_exists"
    assert error.value.response()["error"]["existing"][0]["id"] == str(first["id"])
    assert first["identifiers"]["arxiv"] == "math.DG/0211159v1"
    assert arxiv_key(first["identifiers"]["arxiv"]) == "math/0211159"
    other_project = create(world.conn, "project", slug="another-project", title="Another")
    other = create_catalog(world.conn, world.actor, "reference", data(world, project_id=other_project["id"]))
    assert other["id"] != first["id"]
    with pytest.raises(IntegrityError), world.conn.begin_nested():
        create(world.conn, "reference", **data(world, cite_key="direct-duplicate", identifiers={"arxiv": "math/0211159v3"}))


def test_catalog_completeness_and_duplicate_updates(world):
    first = create_catalog(world.conn, world.actor, "reference", data(world, authors=[], issued_year=None))
    assert first["status"] == "incomplete"
    complete = update_catalog(world.conn, world.actor, "reference", first["id"], CatalogUpdate(expected_revision=first["revision"],
        changes={"authors": ["Perelman, Grigori"], "issued_year": 2002}))
    assert complete["status"] == "active"
    withdrawn = update_catalog(world.conn, world.actor, "reference", first["id"], CatalogUpdate(expected_revision=complete["revision"],
        changes={"status": "withdrawn"}))
    assert withdrawn["status"] == "withdrawn"
    other = create_catalog(world.conn, world.actor, "reference", data(world, cite_key="other", identifiers={"doi": "10.1000/other"}))
    with pytest.raises(DomainError) as error:
        update_catalog(world.conn, world.actor, "reference", other["id"], CatalogUpdate(expected_revision=other["revision"],
            changes={"identifiers": {"arxiv": "math/0211159v2"}}))
    assert error.value.code == "reference_exists"


def test_citation_usage_is_immutable_and_bibtex_is_structured(world):
    reference = create_catalog(world.conn, world.actor, "reference", data(world, kind="book", venue="A publisher"))
    result = record_usage(world.conn, world.actor, ReferenceUsage(reference_id=reference["id"],
        subject={"kind": "document", "id": world.document["id"]}, locator="Section 3"))
    parsed = parse_string(result["bibtex"], "bibtex").entries[reference["cite_key"]]
    assert parsed.fields["publisher"] == "A publisher"
    assert parsed.fields["eprint"] == "math.DG/0211159v1"
    assert result["usage"]["cited_as"] == reference["cite_key"]
    with pytest.raises(IntegrityError), world.conn.begin_nested():
        world.conn.execute(update(tables["reference_usage"]).where(tables["reference_usage"].c.id == result["usage"]["id"]).values(locator="Changed"))
    assert bibtex([reference]) == bibtex([reference])
    with pytest.raises(DomainError) as error:
        record_usage(world.conn, world.actor, ReferenceUsage(reference_id=reference["id"], subject={"kind": "host", "id": world.host["id"]}))
    assert error.value.code == "scope_mismatch"


def test_catalog_rejects_metadata_that_the_bibtex_writer_cannot_export(world):
    with pytest.raises(DomainError) as error:
        create_catalog(world.conn, world.actor, "reference", data(world, title="An unmatched { bracket"))
    assert error.value.code == "invalid_reference_metadata"
    assert error.value.status == 422
    assert not world.conn.execute(select(tables["reference"].c.id)).first()
