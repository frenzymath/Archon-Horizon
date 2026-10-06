"""Explicit project recipes built through the same domain services as the API."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import re
from typing import Any, Literal
from uuid import UUID, uuid5

from pydantic import Field, field_validator
from sqlalchemy import select

from . import models
from .auth import Actor, require_admin
from .catalog import create_catalog
from .errors import DomainError
from .records import canonical, create, get, json_value, transaction_lock
from .reviewer_guidance import instructions as reviewer_instructions
from .schema import tables


PRESETS = {
    "preprocessing-roadmap": {
        "phase": "preprocessing",
        "instructions": "For milestone projects, approve a cited named route with decomposition review before contract work. Contract PRs need concrete definitions, compiled public Lean statements and current-head approvals from statement-fidelity, definitions, decomposition and library-api. Require the trusted verification receipt and review the integrated endpoint route. Initial human baseline approval precedes a separately launched proof run. Legacy projects retain proportional roadmap review. Make bounded repairs directly, then refresh the required coverage for the final head.",
        "reviewers": {
            name: reviewer_instructions(name)
            for name in ("statement-fidelity", "decomposition", "definitions", "library-api")
        },
    },
    "formalization-roadmap": {
        "phase": "formalization",
        "instructions": "Keep workspace proof work free of library publication gates. Review verified graph/progress changes proportionately and keep helper ownership separate from logical prerequisites. In milestone projects, frozen statement, definition, pin or route changes require an evidence-based correction and the full strict contract panel, followed by explicit baseline adoption. Preserve conditional proof status and require Comparator/axiom evidence for completion claims. Make straightforward graph fixes directly.",
        "reviewers": {
            "roadmap-consistency": reviewer_instructions("roadmap-consistency"),
            **{name: reviewer_instructions(name) for name in
               ("statement-fidelity", "decomposition", "definitions", "library-api")},
        },
    },
    "postprocessing-library": {
        "phase": "postprocessing",
        "instructions": (
            "Follow horizon-pipeline's postprocessing phase skill. For already proved input, adapt coherent "
            "result families with their proofs; review public statements and definitions first within each PR. "
            "A separate statement prototype is useful for materially uncertain interfaces, not mandatory for "
            "every port. Explicit sorry proofs require a README admission ledger, honest status and owned follow-up. "
            "Public mathematical design is the primary objective: review fidelity, useful generality, definitions, "
            "hypotheses, signatures, naming, structure and existing Mathlib alternatives strictly. Cheap useful "
            "improvements require repair or a concrete mathematical or compatibility tradeoff; existing consumers "
            "compiling does not make them optional. Maintainers make bounded repairs directly on the existing PR, "
            "coordinate ownership and refresh affected current-head review. Reuse sound existing source proofs "
            "and port independent families in parallel; proof micro-optimization is secondary to public design. "
            "Every enabled policy dimension requires delivered independent current-head coverage or explicitly "
            "validated carry-forward of unchanged specialist evidence. Prior objections remain until independently "
            "resolved. Shadow review plans measure scope reduction without relaxing the enforced panel. "
            "Each reviewer publishes under its own Forge identity using horizon-communication and cumulative PR "
            "discussion. Prefer typed assessments: lead with actionable findings, keep clean approvals very brief, "
            "and link detailed evidence. Existing workspace code is input, not a required architecture. "
            "No separately accepted roadmap is required."
        ),
        "reviewers": {
            name: reviewer_instructions(name)
            for name in ("mathematical-fidelity", "definitions", "library-api", "library-architecture",
                         "lean-proof-quality", "lean-performance", "repository-quality", "scholarly-quality")
        },
    },
}

ALLOWED = set(models.CREATE_MODELS) - {"run", "assignment", "automation", "obligation", "subscription"}


class RecipeRecord(models.Contract):
    key: models.Slug
    kind: str
    values: dict[str, Any]

    @field_validator("kind")
    @classmethod
    def known_kind(cls, value):
        if value not in ALLOWED:
            raise ValueError("recipe kind is not a setup catalog record")
        return value


class ReviewPreset(models.Contract):
    key: models.Slug
    preset: Literal["preprocessing-roadmap", "formalization-roadmap", "postprocessing-library"]
    project_id: Any
    repository_id: Any
    maintainer_identity_id: Any = None
    invocation: Literal["assignment", "subrequest"] = "assignment"


class ProjectRecipe(models.Contract):
    schema_version: Literal[1] = 1
    id: UUID
    records: list[RecipeRecord] = Field(min_length=1, max_length=100)
    review_presets: list[ReviewPreset] = Field(default_factory=list, max_length=12)


class RunRecipe(models.Contract):
    schema_version: Literal[1] = 1
    id: UUID
    run: models.RunCreate


def launch(conn, actor, scheduler, recipe: RunRecipe):
    require_admin(conn, actor)
    transaction_lock(conn)
    digest = hashlib.sha256(canonical(recipe.model_dump())).hexdigest()
    table = tables["api_request"]
    previous = conn.execute(select(table).where(table.c.principal_id == actor.id,
        table.c.operation == "launch_run", table.c.idempotency_key == str(recipe.id))).mappings().first()
    if previous:
        if previous["request_sha256"] != digest:
            raise DomainError("recipe_conflict", "This launch ID was already used with different run settings")
        return previous["response"]
    result = json_value(scheduler.run(conn, actor, recipe.run))
    create(conn, "api_request", principal_id=actor.id, operation="launch_run", idempotency_key=str(recipe.id),
        request_sha256=digest, status="completed", response=result,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=scheduler.service.config.storage.idempotency_retention_seconds))
    return result


def expanded(recipe: ProjectRecipe):
    records = list(recipe.records)
    for selected in recipe.review_presets:
        preset = PRESETS[selected.preset]
        reviewers = []
        for name, instruction in preset["reviewers"].items():
            key = f"{selected.key}-{name}"
            records.append(RecipeRecord(key=key, kind="reviewer_descriptor", values={
                "project_id": selected.project_id, "slug": key, "instructions": instruction,
                "functions": ["reviewer"], "invocation": selected.invocation}))
            reviewers.append({"$ref": key})
        records.append(RecipeRecord(key=selected.key, kind="review_policy", values={
            "project_id": selected.project_id, "slug": selected.key, "repository_ids": [selected.repository_id],
            "phases": [preset["phase"]], "instructions": preset["instructions"],
            "reviewer_descriptor_ids": reviewers, "maintainer_identity_id": selected.maintainer_identity_id}))
    keys = [record.key for record in records]
    if len(keys) != len(set(keys)) or "operator" in keys:
        raise ValueError("recipe record keys must be unique; operator is reserved")
    return records


def resolve(value, references):
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            key = value["$ref"]
            if key not in references:
                raise ValueError(f"reference must name an earlier record with an ID: {key}")
            return references[key]
        return {key: resolve(item, references) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve(item, references) for item in value]
    return value


def preview(recipe: ProjectRecipe):
    references = {"operator": uuid5(recipe.id, "operator")}
    records = expanded(recipe)
    for record in records:
        values = resolve(record.values, references)
        models.CREATE_MODELS[record.kind].model_validate(values)
        for name in ("source_commit_oid", "base_commit_oid", "head_commit_oid"):
            if values.get(name) and not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", values[name]):
                raise ValueError(f"{record.key}.{name} must pin a complete Git commit identity")
        credential = values.get("credential_ref")
        if credential and not re.fullmatch(r"secret:[A-Za-z0-9_-]{1,100}", credential):
            raise ValueError("recipe credentials must be opaque secret:<name> references")
        if record.kind != "host_harness":
            references[record.key] = uuid5(recipe.id, record.key)
    expanded_values = [record.model_dump(mode="json") for record in records]
    return {"schema_version": 1, "id": str(recipe.id), "sha256": hashlib.sha256(canonical({"id": recipe.id, "records": expanded_values})).hexdigest(),
            "records": expanded_values,
            "effects": ["create configured database records atomically"],
            "external_effects": [], "starts_runs": False}


def operator(conn, username: str) -> Actor:
    row = conn.execute(select(tables["principal"]).where(tables["principal"].c.username == username,
        tables["principal"].c.kind == "human", tables["principal"].c.disabled_at.is_(None))).mappings().first()
    if row is None:
        raise DomainError("operator_not_found", "Create the named local administrator before applying a setup recipe", 404)
    actor = Actor(row["id"], "human", {"username": username}, "operator_cli")
    require_admin(conn, actor)
    return actor


def apply(conn, actor, service, recipe: ProjectRecipe):
    plan = preview(recipe)
    require_admin(conn, actor)
    transaction_lock(conn)
    request = tables["api_request"]
    previous = conn.execute(select(request).where(request.c.principal_id == actor.id,
        request.c.operation == "bootstrap_project", request.c.idempotency_key == str(recipe.id))).mappings().first()
    if previous:
        if previous["request_sha256"] != plan["sha256"]:
            raise DomainError("recipe_conflict", "This recipe ID was already applied with different contents; use a new reviewed recipe ID")
        return previous["response"]
    references = {"operator": actor.id}
    result = {}
    for record in expanded(recipe):
        values = resolve(record.values, references)
        if record.kind == "project":
            row = service.project(conn, actor, models.ProjectCreate.model_validate(values))
        elif record.kind == "mission":
            row = service.mission(conn, actor, models.MissionCreate.model_validate(values))
        else:
            row = create_catalog(conn, actor, record.kind, values)
        if "id" in row:
            references[record.key] = row["id"]
        result[record.key] = {key: json_value(row[key]) for key in ("id", "number", "slug", "host_id", "harness_id") if key in row}
    for selected in recipe.review_presets:
        repository = get(conn, "repository", resolve(selected.repository_id, references))
        expected = "library" if selected.preset == "postprocessing-library" else "knowledge"
        if repository["purpose"] != expected:
            raise DomainError("preset_scope_mismatch", f"{selected.preset} requires a {expected} repository")
    response = {"id": str(recipe.id), "status": "completed", "records": result}
    create(conn, "api_request", principal_id=actor.id, operation="bootstrap_project", idempotency_key=str(recipe.id),
        request_sha256=plan["sha256"], status="completed", response=response,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=service.config.storage.idempotency_retention_seconds))
    return response
