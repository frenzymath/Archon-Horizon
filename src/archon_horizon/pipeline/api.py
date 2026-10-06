"""FastAPI boundary for the explicitly configured pipeline installation."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from pathlib import Path
import time
from uuid import UUID
from urllib.parse import urlencode
from typing import Literal

from fastapi import FastAPI, Request, Query
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, OperationalError, TimeoutError as PoolTimeout

from . import models, readmodels, milestones, milestone_jobs
from .artifacts import ArtifactStore
from .auth import authenticate, is_admin, live_execution, login, require_admin, require_host, require_project
from .catalog import CatalogUpdate, UPDATE_FIELDS, configure_host_harness, create_catalog, update_catalog
from .commands import COMMAND_TARGETS, Command, execute
from .config import PipelineConfig
from .database import Database
from .errors import DomainError
from .records import canonical, change, create, get, json_value, project_of, save_blob, scoped_query, transaction_lock
from .scheduler import Scheduler
from .schema import tables
from .service import Service
from .mission_tree import contains_mission, require_mission_authority
from .telemetry import RequestTelemetry, make_tracer_provider
from .worker_events import WorkerOperation, handle as handle_worker

log = logging.getLogger(__name__)


def create_app(config: PipelineConfig, *, database: Database | None = None, background: bool = True) -> FastAPI:
    owned = database is None
    db = database or Database(config.database_url.get_secret_value(), pool_size=config.database_pool_size,
        pool_timeout=config.database_pool_timeout_seconds,
        statement_timeout_ms=config.statement_timeout_seconds * 1000)
    store = ArtifactStore(config.artifact_root)
    service = Service(store, config)
    scheduler = Scheduler(service)
    stop = asyncio.Event()
    connectors = None
    search_manager = None
    tracer_provider = make_tracer_provider()

    def provision_review_accounts(project_id, *, refresh_access=False):
        if config.reviewer_account_admin_credentials:
            from .reviewer_accounts import ensure_project_accounts
            return ensure_project_accounts(db, service, project_id, refresh_access=refresh_access)

    def provision_for_request(request, parent_id):
        with db.transaction() as conn:
            actor = actor_for(conn, request)
            project_id = project_of(conn, "provider_request", parent_id)
            require_project(conn, actor, project_id, "maintainer")
        provision_review_accounts(project_id)

    def provision_catalog_result(kind, result):
        provision_review_accounts(UUID(str(result["project_id"])), refresh_access=True)
        # Provisioning may link an identity and advance the descriptor revision.
        with db.transaction() as conn:
            return {**result, **get(conn, kind, result["id"])}

    def tick():
        with db.transaction() as conn:
            if transaction_lock(conn, wait=False):
                scheduler.tick(conn)

    async def watchdog():
        while not stop.is_set():
            try:
                await asyncio.to_thread(tick)
            except Exception:
                log.exception("Pipeline watchdog reconciliation failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=config.scheduler_interval_seconds)
            except TimeoutError:
                pass

    async def connector_loop(kind: str | None):
        while not stop.is_set():
            failed = False
            try:
                if kind:
                    result = await asyncio.to_thread(connectors.sync_all, kind=kind)
                    failed = any(status != "current" for status in result.values())
                else:
                    await asyncio.to_thread(connectors.dispatch_one)
            except Exception:
                failed = True
                log.exception("Connector reconciliation failed")
            try:
                # Zulip's bounded long poll maintains its event-queue lease.
                interval = (1 if kind == "zulip" and not failed else
                            config.connector_interval_seconds if kind else 1)
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                pass

    async def retention_loop():
        from .storage import apply_database_retention, database_retention_preview
        def prune_batch():
            with db.transaction() as conn:
                if not transaction_lock(conn, wait=False):
                    return
                preview = database_retention_preview(conn, config)
                return apply_database_retention(conn, config, preview)
        while not stop.is_set():
            try:
                await asyncio.to_thread(prune_batch)
            except Exception:
                log.exception("Bounded transport retention failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=300)
            except TimeoutError:
                pass

    @asynccontextmanager
    async def lifespan(app):
        nonlocal connectors, search_manager
        await asyncio.to_thread(db.check_revision)
        tasks = []
        if background:
            from .connectors import ConnectorManager, SecretResolver
            connectors = ConnectorManager(db, service, SecretResolver(config.state_root), should_stop=stop.is_set)
            tasks = [asyncio.create_task(watchdog()), asyncio.create_task(connector_loop("forge")),
                     asyncio.create_task(connector_loop("zulip")),
                     asyncio.create_task(connector_loop(None)), asyncio.create_task(retention_loop())]
        if config.search_enabled:
            from .search import SearchManager
            from .connectors import SecretResolver
            secrets = SecretResolver(config.state_root)
            def search_credential(source):
                with db.transaction() as conn:
                    repository = get(conn, "repository", source.source_id)
                    if str(repository["project_id"]) != source.project_id:
                        raise ValueError("search source scope changed")
                    integration = get(conn, "integration", repository["integration_id"])
                    expected_url = integration["endpoint"].rstrip("/") + "/" + repository["remote_path"].strip("/") + ".git"
                    if source.url != expected_url or not integration["enabled"]:
                        raise ValueError("search integration is unavailable")
                secret = secrets(integration["credential_ref"])
                return "token " + secret["token"] if secret.get("token") else None
            search_manager = SearchManager(config.state_root / "cache" / "search",
                allowed_origins=config.search_allowed_origins, max_workers=config.search_workers,
                memory_budget_bytes=config.search_memory_budget_bytes, disk_budget_bytes=config.storage.cache_budget_bytes,
                local_source_root=config.search_local_roots[0] if config.search_local_roots else None,
                credential_resolver=search_credential)
        try:
            yield
        finally:
            stop.set()
            for task in tasks:
                await task
            if search_manager:
                await asyncio.to_thread(search_manager.close)
            if owned:
                db.close()
            tracer_provider.shutdown()

    app = FastAPI(title="Archon Horizon Pipeline", version="3", lifespan=lifespan)
    app.state.database, app.state.service, app.state.scheduler = db, service, scheduler
    app.state.shutdown_event = stop

    @app.exception_handler(DomainError)
    async def domain_error(request, error):
        return JSONResponse(error.response(), status_code=error.status, headers={"Cache-Control": "no-store"})

    @app.exception_handler(ValidationError)
    async def validation_error(request, error):
        return JSONResponse({"error": {"code": "validation_failed", "message": "Request does not match the contract",
                             "fields": [{"path": list(item["loc"]), "message": item["msg"]} for item in error.errors()]}},
                            status_code=422, headers={"Cache-Control": "no-store"})

    @app.exception_handler(json.JSONDecodeError)
    async def invalid_json(request, error):
        return JSONResponse({"error": {"code": "invalid_json", "message": "Request body must be valid JSON"}}, status_code=422)

    @app.exception_handler(IntegrityError)
    async def integrity_error(request, error):
        return JSONResponse({"error": {"code": "constraint_conflict", "message": "The operation conflicts with current records"}},
                            status_code=409, headers={"Cache-Control": "no-store"})

    async def unavailable(request, error):
        return JSONResponse({"error": {"code": "backend_unavailable", "message": "The control plane is temporarily unavailable"}},
                            status_code=503, headers={"Cache-Control": "no-store", "Retry-After": "2"})
    app.add_exception_handler(OperationalError, unavailable)
    app.add_exception_handler(PoolTimeout, unavailable)

    @app.middleware("http")
    async def request_boundary(request, call_next):
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            length = request.headers.get("content-length")
            if length and (not length.isdecimal() or int(length) > config.max_request_bytes):
                return JSONResponse({"error": {"code": "request_too_large", "message": "Request exceeds the upload limit"}}, status_code=413)
            chunks, count = [], 0
            async for chunk in request.stream():
                count += len(chunk)
                if count > config.max_request_bytes:
                    return JSONResponse({"error": {"code": "request_too_large", "message": "Request exceeds the upload limit"}}, status_code=413)
                chunks.append(chunk)
            request._body = b"".join(chunks)
            origin = request.headers.get("origin")
            if origin is not None and origin.rstrip("/") != config.public_url.rstrip("/"):
                return JSONResponse({"error": {"code": "invalid_origin", "message": "Cross-origin writes are not permitted"}}, status_code=403)
            if request.cookies.get("horizon_session") and not request.headers.get("authorization") and not origin:
                return JSONResponse({"error": {"code": "origin_required", "message": "Browser writes require their origin"}}, status_code=403)
        response = await call_next(request)
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    app.add_middleware(RequestTelemetry, provider=tracer_provider)

    def actor_for(conn, request):
        auth = request.headers.get("authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else request.cookies.get("horizon_session")
        return authenticate(conn, token)

    def respond(value):
        return JSONResponse(json_value(value))

    def private_read(request, actor, value):
        body = canonical(value)
        etag = '"' + hashlib.sha256(str(actor.id).encode() + b":representation-v1:" + body).hexdigest() + '"'
        headers = {"ETag": etag, "Cache-Control": "private, no-cache", "Vary": "Cookie, Authorization"}
        if etag in {tag.strip().removeprefix("W/") for tag in request.headers.get("if-none-match", "").split(",")}:
            return Response(status_code=304, headers=headers)
        return Response(body, media_type="application/json", headers=headers)

    @app.get("/api/v3/instruction-catalog")
    def instruction_catalog(request: Request):
        from .instruction_catalog import read_catalog
        with db.transaction() as conn:
            actor = actor_for(conn, request)
            require_admin(conn, actor)
        return private_read(request, actor, read_catalog(config.skill_source_root))

    @app.get("/api/v3/instruction-catalog/file")
    def instruction_file(request: Request, path: str = Query(min_length=1, max_length=512)):
        from .instruction_catalog import read_catalog
        with db.transaction() as conn:
            actor = actor_for(conn, request)
            require_admin(conn, actor)
        return private_read(request, actor, read_catalog(config.skill_source_root, path=path))

    async def project_mutation(request, operation, raw, scope_kind, scope_id, perform, *, role="worker"):
        def work():
            with db.transaction() as conn:
                transaction_lock(conn)
                actor = actor_for(conn, request)
                project_id = project_of(conn, scope_kind, scope_id)
                require_project(conn, actor, project_id, role)
                return idempotent(conn, actor, request, operation, raw, project_id, lambda: perform(conn, actor))
        return respond(await asyncio.to_thread(work))

    def idempotent(conn, actor, request, operation, payload, project_id, perform):
        key = request.headers.get("idempotency-key")
        if not key or len(key) > 200:
            raise DomainError("idempotency_required", "Mutations require an Idempotency-Key of at most 200 characters", 422)
        digest = hashlib.sha256(canonical(payload)).hexdigest()
        table = tables["api_request"]
        previous = conn.execute(receipt_query(conn, actor, operation, key)).mappings().first()
        if previous:
            if actor.kind == "host" and operation in ("worker_operation", "worker_claim", "milestone_check", "milestone_job_finish"):
                pass  # These routes authenticate the recorded host before receipt lookup.
            elif previous["project_id"]:
                require_project(conn, actor, previous["project_id"])
            else:
                require_admin(conn, actor)
            if previous["request_sha256"] != digest:
                raise DomainError("idempotency_conflict", "This request key was already used with different content")
            if previous["status"] != "completed":
                raise DomainError("operation_unsettled", "The original operation has not settled")
            if previous.get("response") is not None:
                return previous["response"]
            blob = get(conn, "artifact", previous["response_artifact_id"])
            return json.loads(store.read(blob["content"]["sha256"], blob["content"]["size_bytes"]))
        record = create(conn, "api_request", principal_id=actor.id, project_id=project_id, operation=operation,
            idempotency_key=key, request_sha256=digest,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=config.storage.idempotency_retention_seconds))
        result = json_value(perform())
        if len(canonical(result)) <= 16384:
            change(conn, "api_request", record["id"], status="completed", response=result)
        elif project_id:
            artifact = save_blob(conn, store, project_id, result)
            change(conn, "api_request", record["id"], status="completed", response_artifact_id=artifact["id"])
        else:
            raise DomainError("response_too_large", "Installation command returned an oversized result", 500)
        return result

    def receipt_query(conn, actor, operation, key):
        table = tables["api_request"]
        principals = [actor.id]
        if actor.kind == "agent":
            current = live_execution(conn, actor)
            principal, execution = tables["principal"], tables["execution"]
            principals = select(principal.c.id).join(execution, principal.c.execution_id == execution.c.id).where(
                execution.c.assignment_id == current["assignment_id"])
        return select(table).where(table.c.principal_id.in_(principals),
            table.c.operation == operation, table.c.idempotency_key == key).order_by(table.c.created_at)

    @app.get("/health/live")
    def live():
        return {"status": "alive"}

    @app.get("/health/ready")
    def ready():
        with db.transaction() as conn:
            conn.execute(select(1))
        return {"status": "ready"}

    attempts: dict[str, list[float]] = {}
    @app.post("/api/v3/auth/login")
    async def sign_in(request: Request):
        data = await request.json()
        if set(data) != {"username", "password"} or not all(isinstance(value, str) for value in data.values()):
            raise DomainError("invalid_credentials", "Username and password are required", 422)
        peer = request.client.host if request.client else "unknown"
        now = time.monotonic()
        recent = [value for value in attempts.get(peer, []) if value > now - 60]
        if len(recent) >= 10:
            raise DomainError("rate_limited", "Wait before trying to sign in again", 429)
        if len(attempts) > 10000:
            attempts.clear()
        attempts[peer] = [*recent, now]
        def work():
            with db.transaction() as conn:
                row, token = login(conn, data["username"], data["password"], session_seconds=config.session_seconds)
                return row, token
        row, token = await asyncio.to_thread(work)
        response = respond({"id": row["id"], "username": row["username"],
                            "max_offline_replay_seconds": config.storage.max_offline_replay_seconds})
        response.set_cookie("horizon_session", token, httponly=True, secure=config.secure_cookies,
                            samesite="strict", max_age=config.session_seconds, path="/")
        return response

    @app.get("/api/v3/auth/me")
    def me(request: Request):
        with db.transaction() as conn:
            actor = actor_for(conn, request)
            admin = is_admin(conn, actor)
            grant = tables["project_grant"]
            roles = set(conn.execute(select(grant.c.role).where(grant.c.principal_id == actor.id)).scalars())
            return respond({"id": actor.id, "username": actor.owner.get("username", actor.kind),
                            "max_offline_replay_seconds": config.storage.max_offline_replay_seconds,
                            "role": "admin" if admin else "maintainer" if "maintainer" in roles else "worker" if "worker" in roles else "viewer",
                            "permissions": {"write": admin or bool(roles & {"worker", "maintainer"}), "admin": admin}})

    @app.post("/api/v3/auth/logout")
    def sign_out(request: Request):
        with db.transaction() as conn:
            actor = actor_for(conn, request)
            change(conn, "credential", actor.credential_id, revoked_at=func.now())
        response = respond({"status": "signed_out"})
        response.delete_cookie("horizon_session", path="/")
        return response

    @app.get("/api/v3/projects")
    def projects(request: Request, cursor: str | None = None, limit: int = Query(50, ge=1, le=100)):
        with db.transaction() as conn:
            actor = actor_for(conn, request)
            return respond(readmodels.page(conn, readmodels.visible_projects(conn, actor), tables["project"], cursor, limit, order="number"))

    @app.get('/api/v3/documents/{identifier}/milestones')
    def objective_milestones(identifier: UUID, request: Request):
        with db.transaction() as conn:
            return respond(milestones.objective_view(conn, actor_for(conn, request), identifier, config))

    @app.post('/api/v3/milestones/baselines')
    async def approve_milestones(request: Request, data: milestones.AcceptBaseline):
        return await project_mutation(request, 'approve_milestones', data.model_dump(mode='json'),
            'document', data.document_id, lambda conn, actor: milestones.accept_baseline(conn, actor, service, data), role='maintainer')

    @app.post('/api/v3/worker/milestone-checks')
    async def milestone_check(request: Request, data: milestones.CheckSubmission):
        def work():
            with db.transaction() as conn:
                transaction_lock(conn)
                actor = actor_for(conn, request)
                workspace = get(conn, 'workspace', data.workspace_id)
                require_host(conn, actor, workspace['host_id'])
                return idempotent(conn, actor, request, 'milestone_check', data.model_dump(mode='json'),
                    workspace['project_id'], lambda: milestones.register_check(conn, actor, data))
        return respond(await asyncio.to_thread(work))

    @app.post('/api/v3/milestones/verifications')
    async def request_milestone_verification(request: Request, data: milestone_jobs.VerificationRequest):
        return await project_mutation(request, 'milestone_verification', data.model_dump(mode='json'),
            'workspace', data.workspace_id, lambda conn, actor: milestone_jobs.queue(conn, actor, data))

    @app.get('/api/v3/milestones/verifications/{identifier}')
    def read_milestone_verification(identifier: UUID, request: Request):
        with db.transaction() as conn:
            row = get(conn, 'milestone_job', identifier)
            require_project(conn, actor_for(conn, request), row['project_id'])
            return respond({key: value for key, value in row.items() if key != 'claim_token'})

    @app.get('/api/v3/milestones/checks/{identifier}')
    def read_milestone_check(identifier: UUID, request: Request):
        with db.transaction() as conn:
            row = get(conn, 'milestone_check', identifier)
            require_project(conn, actor_for(conn, request), row['project_id'])
            return respond(row)

    @app.post('/api/v3/worker/milestone-jobs/claim')
    def claim_milestone_job(request: Request, data: milestone_jobs.Claim):
        with db.transaction() as conn:
            transaction_lock(conn)
            return respond({'job': milestone_jobs.claim(conn, actor_for(conn, request), data)})

    @app.post('/api/v3/worker/milestone-jobs/{identifier}/heartbeat')
    def heartbeat_milestone_job(identifier: UUID, request: Request, data: milestone_jobs.Lease):
        with db.transaction() as conn:
            transaction_lock(conn)
            return respond(milestone_jobs.heartbeat(conn, actor_for(conn, request), identifier, data))

    @app.post('/api/v3/worker/milestone-jobs/{identifier}/finish')
    def finish_milestone_job(identifier: UUID, request: Request, data: milestone_jobs.Finish):
        with db.transaction() as conn:
            transaction_lock(conn)
            actor = actor_for(conn, request)
            job = get(conn, 'milestone_job', identifier)
            require_host(conn, actor, job['host_id'])
            raw = {'id': str(identifier), **data.model_dump(mode='json')}
            return respond(idempotent(conn, actor, request, 'milestone_job_finish', raw, job['project_id'],
                lambda: milestone_jobs.finish(conn, actor, identifier, data)))

    @app.get("/api/v3/schema")
    def schema(request: Request, section: str | None = None, name: str | None = None):
        from .command_args import COMMAND_ARGS
        from .record_queries import supported_filters, validate_query
        from .schema_discovery import canonical_query_name, canonicalize_published_queries, lookup_error
        validate_query(request, {"section", "name"})
        from .communications import DiscussionRegistration, DiscussionSubjectLink
        from .notifications import OperatorNotice
        from .content import BlobUpload
        from .references import ReferenceUsage
        from .reviewer_invocations import ReviewerPrepare, ReviewerAttach, ReviewerCancel, ReviewerReport
        with db.transaction() as conn:
            actor_for(conn, request)
        openapi = app.openapi()
        contracts = {"create": {kind: model.model_json_schema() for kind, model in models.CREATE_MODELS.items()},
                        "command_args": {op: model.model_json_schema() for op, model in COMMAND_ARGS.items()},
                        "record_filters": {kind: supported_filters(kind) for kind in sorted(readable)},
                        "queries": {f"{method.upper()} {path}": {
                            "parameters": operation.get("parameters", []),
                            "additional_query_parameters": False if path in {
                                "/api/v3/records/{kind}", "/api/v3/schema", "/api/v3/repositories/{identifier}/head",
                                "/api/v3/repositories/{identifier}/file", "/api/v3/forge-items/{identifier}/inspect",
                                "/api/v3/assignments", "/api/v3/forge-items", "/api/v3/discussions"} else None,
                        } for path, methods in openapi["paths"].items() for method, operation in methods.items()
                          if method == "get" and path.startswith("/api/v3/")},
                        "catalog_update": CatalogUpdate.model_json_schema(),
                        "catalog_update_fields": {kind: sorted(fields) for kind, fields in UPDATE_FIELDS.items()},
                        "mission_update": models.MissionUpdate.model_json_schema(),
                        "obligation_resolve": models.ObligationResolve.model_json_schema(),
                        "command": Command.model_json_schema(),
                        "commands": COMMAND_TARGETS,
                        "operations": {name: contract.model_json_schema() for name, contract in {
                            "approve_milestones": milestones.AcceptBaseline,
                            "milestone_check": milestones.CheckSubmission,
                            "milestone_verification": milestone_jobs.VerificationRequest,
                            "register_discussion": DiscussionRegistration, "link_discussion_subject": DiscussionSubjectLink,
                            "operator_notice": OperatorNotice,
                            "upload_blob": BlobUpload, "cite_reference": ReferenceUsage,
                            "prepare_reviewer": ReviewerPrepare, "prepare_reviewer_assignment": ReviewerPrepare,
                            "attach_reviewer": ReviewerAttach, "cancel_reviewer": ReviewerCancel,
                            "reviewer_report": ReviewerReport,
                            "forge_create": models.ForgeCreate, "forge_comment": models.ForgeComment,
                            "forge_change": models.ForgeChange, "forge_edit": models.ForgeEdit,
                            "forge_review": models.ForgeReview, "forge_merge": models.ForgeMerge, "forge_label": models.ForgeLabel,
                            "review_carry_forward_evidence": models.ReviewCarryForwardEvidence,
                        }.items()},
                        "routes": {"create": "POST /api/v3/records/{kind}", "read": "GET /api/v3/records/{kind}/{id}",
                                   "objective_milestones": "GET /api/v3/documents/{id}/milestones",
                                   "approve_milestones": "POST /api/v3/milestones/baselines",
                                   "milestone_verification": "POST /api/v3/milestones/verifications",
                                   "milestone_verification_status": "GET /api/v3/milestones/verifications/{id}",
                                   "milestone_check": "GET /api/v3/milestones/checks/{id}",
                                   "list": "GET /api/v3/records/{kind}?project_id=...",
                                   "catalog_update": "PATCH /api/v3/records/{kind}/{id}",
                                   "register_discussion": "POST /api/v3/discussions",
                                   "discussion_topics": "GET /api/v3/discussions/{id}/topics",
                                   "discussion_search": "GET /api/v3/discussions/{id}/search",
                                   "discussion_delta": "GET /api/v3/discussions/{id}/messages?assignment_id=...&unread_only=true",
                                   "operator_notice": "POST /api/v3/assignments/{id}/control-notices",
                                   "control_notices": "GET /api/v3/assignments/{id}/control-notices",
                                   "notification_detail": "GET /api/v3/notifications/{id}",
                                   "link_discussion_subject": "POST /api/v3/discussion-subjects",
                                   "upload_blob": "POST /api/v3/artifacts",
                                   "blob_content": "GET /api/v3/artifacts/{id}/content",
                                   "cite_reference": "POST /api/v3/reference-usages",
                                   "prepare_reviewer": "POST /api/v3/reviewer-invocations",
                                   "prepare_reviewer_assignment": "POST /api/v3/reviewer-assignments",
                                   "forge_change": "POST /api/v3/forge/change", "forge_edit": "POST /api/v3/forge/edit",
                                   "repository_head": "GET /api/v3/repositories/{id}/head?branch=...",
                                   "repository_file": "GET /api/v3/repositories/{id}/file?commit_oid=...&path=...",
                                   "forge_inspect": "GET /api/v3/forge-items/{id}/inspect?view=...&expected_head_oid=...",
                                   "attach_reviewer": "POST /api/v3/reviewer-invocations/{id}/attach",
                                   "cancel_reviewer": "POST /api/v3/reviewer-invocations/{id}/cancel",
                                   "reviewer_report": "POST /api/v3/reviewer-invocations/{id}/report",
                                   "reviewer_accounts": "GET /api/v3/executions/{id}/reviewer-accounts",
                                   "forge_create": "POST /api/v3/forge/create",
                                   "forge_comment": "POST /api/v3/forge/comment",
                                   "forge_review": "POST /api/v3/forge/review",
                                   "forge_merge": "POST /api/v3/forge/merge",
                                   "forge_label": "POST /api/v3/forge/label",
                                   "mission_update": "PATCH /api/v3/missions/{id}",
                                   "obligation_resolve": "POST /api/v3/obligations/{id}/resolve",
                                   "commands": "POST /api/v3/commands"}}
        canonicalize_published_queries(contracts)
        if name and not section:
            raise lookup_error(contracts, section, name)
        if section:
            if section not in contracts:
                raise lookup_error(contracts, section, name)
            selected = contracts[section]
            if name:
                selected_name = canonical_query_name(contracts, name) if section == "queries" else name
                if not isinstance(selected, dict) or selected_name not in selected:
                    raise lookup_error(contracts, section, name)
                selected = selected[selected_name]
            return respond(selected)
        return respond(contracts)

    @app.get("/api/v3/projects/{identifier}")
    def project_detail(identifier: UUID, request: Request):
        with db.transaction() as conn:
            actor = actor_for(conn, request)
            require_project(conn, actor, identifier)
            return private_read(request, actor, get(conn, "project", identifier))

    @app.get("/api/v3/runs")
    def list_runs(request: Request, project_id: UUID, cursor: str | None = None, limit: int = Query(50, ge=1, le=100)):
        with db.transaction() as conn:
            actor = actor_for(conn, request)
            return private_read(request, actor, readmodels.runs(conn, actor, project_id, cursor, limit))

    @app.get("/api/v3/runs/{identifier}")
    def run_detail(identifier: UUID, request: Request):
        with db.transaction() as conn:
            actor = actor_for(conn, request)
            return private_read(request, actor, readmodels.run_detail(conn, actor, identifier))

    @app.get("/api/v3/assignments")
    def assignments(request: Request, run_id: UUID, cursor: str | None = None, limit: int = Query(50, ge=1, le=100), q: str = "", status: str | None = None):
        from .record_queries import validate_query
        validate_query(request, {"run_id", "cursor", "limit", "q", "status"})
        with db.transaction() as conn:
            actor = actor_for(conn, request)
            return private_read(request, actor, readmodels.assignments(conn, actor, service, run_id, cursor, limit, q, status=status))

    @app.get("/api/v3/assignments/{identifier}")
    def assignment_detail(identifier: UUID, request: Request):
        with db.transaction() as conn:
            actor = actor_for(conn, request)
            return private_read(request, actor, readmodels.assignment_detail(conn, actor, service, identifier))

    @app.get("/api/v3/assignments/{identifier}/context")
    def context(identifier: UUID, request: Request, view: Literal["brief", "full", "operations"] = "brief"):
        with db.transaction() as conn:
            actor = actor_for(conn, request)
            return private_read(request, actor, service.context(conn, actor, identifier, view=view))

    @app.get("/api/v3/projects/{project_id}/dashboard/overview")
    def project_dashboard_overview(request: Request, project_id: UUID):
        from . import dashboard_projects
        with db.transaction() as conn:
            return respond(dashboard_projects.overview(conn, actor_for(conn, request), project_id))

    @app.get("/api/v3/projects/{project_id}/dashboard/nodes")
    def project_dashboard_nodes(request: Request, project_id: UUID, search: str = "", label: str = "",
                                offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100), target_repository_id: UUID | None = None):
        from . import dashboard_projects
        with db.transaction() as conn:
            return respond(dashboard_projects.nodes(conn, actor_for(conn, request), project_id, config,
                search=search, label=label, offset=offset, limit=limit, target_repository_id=target_repository_id))

    @app.get("/api/v3/projects/{project_id}/dashboard/nodes/resolve")
    def project_dashboard_node_summaries(request: Request, project_id: UUID, identifiers: str, target_repository_id: UUID | None = None):
        from . import dashboard_projects
        with db.transaction() as conn:
            return respond(dashboard_projects.node_summaries(conn, actor_for(conn, request), project_id,
                identifiers.split(","), config, target_repository_id=target_repository_id))

    @app.get("/api/v3/projects/{project_id}/dashboard/nodes/{identifier}")
    def project_dashboard_node(request: Request, project_id: UUID, identifier: str, target_repository_id: UUID | None = None):
        from . import dashboard_projects
        with db.transaction() as conn:
            return respond(dashboard_projects.node_detail(conn, actor_for(conn, request), project_id, identifier, config, target_repository_id))

    @app.get("/api/v3/projects/{project_id}/dashboard/objectives")
    def project_dashboard_objectives(request: Request, project_id: UUID):
        from . import dashboard_projects
        with db.transaction() as conn:
            return respond(dashboard_projects.objectives(conn, actor_for(conn, request), project_id, config))

    @app.get("/api/v3/projects/{project_id}/dashboard/objectives/{identifier}")
    def project_dashboard_objective(request: Request, project_id: UUID, identifier: UUID):
        from . import dashboard_projects
        with db.transaction() as conn:
            return respond(dashboard_projects.objectives(conn, actor_for(conn, request), project_id, config, identifier))

    @app.get("/api/v3/projects/{project_id}/dashboard/graph-targets")
    def project_graph_targets(request: Request, project_id: UUID):
        from .graph_progress import target_context
        from .auth import require_project
        with db.transaction() as conn:
            require_project(conn, actor_for(conn, request), project_id)
            return respond(target_context(conn, project_id))

    @app.get("/api/v3/projects/{project_id}/dashboard/graph")
    def project_dashboard_graph(request: Request, project_id: UUID, focus: UUID | None = None, target_repository_id: UUID | None = None):
        from . import dashboard_projects
        with db.transaction() as conn:
            return respond(dashboard_projects.graph(conn, actor_for(conn, request), project_id, config, str(focus) if focus else None, target_repository_id))

    @app.get("/api/v3/projects/{project_id}/dashboard/missions")
    def project_dashboard_missions(request: Request, project_id: UUID, search: str = "", status: str = "",
                                  offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
        from . import dashboard_projects
        with db.transaction() as conn:
            return respond(dashboard_projects.missions(conn, actor_for(conn, request), project_id,
                search=search, status=status, offset=offset, limit=limit))

    @app.get("/api/v3/dashboard/activity/runs")
    def desktop_activity_runs(request: Request, project_id: UUID | None = None, status: str | None = None,
                              before: str | None = None, limit: int = Query(25, ge=1, le=100), compact: bool = False):
        from . import dashboard_activity
        with db.transaction() as conn:
            return respond(dashboard_activity.runs(conn, actor_for(conn, request), project_id=project_id,
                           status=status, before=before, limit=limit, compact=compact))

    @app.get("/api/v3/dashboard/activity/runs/{identifier}")
    def desktop_activity_run(identifier: UUID, request: Request, compact: bool = False,
                             view: Literal["full", "metrics", "queue"] = "full",
                             sessions_before: str | None = None,
                             sessions_limit: int = Query(100, ge=1, le=100)):
        from . import dashboard_activity
        with db.transaction() as conn:
            return respond(dashboard_activity.run_detail(conn, actor_for(conn, request), identifier,
                           service=service, compact=compact, view=view,
                           sessions_before=sessions_before, sessions_limit=sessions_limit))

    @app.get("/api/v3/dashboard/activity/sessions/{identifier}")
    def desktop_activity_session(identifier: UUID, request: Request, compact: bool = False,
                                 view: Literal["full", "reports"] = "full"):
        from . import dashboard_activity
        with db.transaction() as conn:
            return respond(dashboard_activity.session_detail(conn, actor_for(conn, request), identifier,
                           store=service.store, service=service, compact=compact, view=view))

    @app.get("/api/v3/dashboard/activity/sessions/{identifier}/events")
    def desktop_activity_events(identifier: UUID, request: Request, before: str | None = None,
                                limit: int = Query(50, ge=1, le=100), compact: bool = False):
        from . import dashboard_activity
        with db.transaction() as conn:
            return respond(dashboard_activity.events(conn, actor_for(conn, request), identifier,
                           store=service.store, before=before, limit=limit, compact=compact))

    @app.get("/api/v3/dashboard/activity/sessions/{identifier}/events/{event_id}")
    def desktop_activity_event(identifier: UUID, event_id: UUID, request: Request):
        from . import dashboard_activity
        with db.transaction() as conn:
            return respond(dashboard_activity.event_detail(conn, actor_for(conn, request), identifier,
                           event_id, store=service.store))

    @app.get("/api/v3/dashboard/activity/sessions/{identifier}/logs")
    def desktop_activity_logs(identifier: UUID, request: Request, limit: int = Query(50, ge=1, le=100)):
        from . import dashboard_activity
        with db.transaction() as conn:
            return respond(dashboard_activity.logs(conn, actor_for(conn, request), identifier, store=service.store, limit=limit))

    @app.get("/api/v3/projects/{project_id}/dashboard/missions/{identifier}")
    def project_dashboard_mission(request: Request, project_id: UUID, identifier: UUID):
        from . import dashboard_projects
        with db.transaction() as conn:
            return respond(dashboard_projects.mission_detail(conn, actor_for(conn, request), project_id, identifier))

    @app.get("/api/v3/integrations/{identifier}/browser-identity")
    def integration_browser_identity(identifier: UUID, request: Request):
        from .browser_integrations import authorize, verify_binding
        with db.transaction() as conn:
            access = authorize(conn, config, request.cookies.get("horizon_session"), identifier,
                               authorization=request.headers.get("authorization"))
        verify_binding(config, access, identifier, request.cookies, request.headers.getlist("cookie"))
        return Response(status_code=204, headers={"X-Horizon-Integration-User": access["identity"],
                                                "Cache-Control": "no-store", "Vary": "Cookie"})

    @app.post("/api/v3/projects/{project_id}/integrations/{identifier}/web-session")
    async def integration_web_session(project_id: UUID, identifier: UUID, request: Request):
        from .browser_integrations import establish
        result, cookies = await asyncio.to_thread(establish, db, config, request.cookies.get("horizon_session"),
            identifier, project_id, authorization=request.headers.get("authorization"))
        response = respond(result)
        for cookie in cookies:
            response.headers.append("Set-Cookie", cookie)
        return response

    @app.get("/api/v3/roadmap")
    def roadmap(request: Request, project_id: UUID, cursor: str | None = None, limit: int = Query(50, ge=1, le=100), run_id: UUID | None = None, q: str = "", target_repository_id: UUID | None = None):
        with db.transaction() as conn:
            actor = actor_for(conn, request)
            data = readmodels.roadmap(conn, actor, project_id, cursor, limit, run_id=run_id, q=q, config=config, target_repository_id=target_repository_id)
            # Revision vector covers the exact representation and authenticated audience.
            etag = '"' + hashlib.sha256(canonical({"actor": actor.id, "data": data})).hexdigest() + '"'
            headers = {"ETag": etag, "Cache-Control": "private, no-cache", "Vary": "Cookie, Authorization"}
            if request.headers.get("if-none-match") == etag:
                return Response(status_code=304, headers=headers)
            return JSONResponse(json_value(data), headers=headers)

    @app.get("/api/v3/changes")
    def changes(request: Request, project_id: UUID, cursor: str | None = None, limit: int = Query(50, ge=1, le=100), q: str = ""):
        with db.transaction() as conn:
            return respond(readmodels.changes(conn, actor_for(conn, request), project_id, cursor, limit, q=q, config=config))

    @app.get("/api/v3/forge-items")
    def forge_items(request: Request, project_id: UUID, cursor: str | None = None, limit: int = Query(50, ge=1, le=100), q: str = ""):
        from .record_queries import validate_query
        validate_query(request, {"project_id", "cursor", "limit", "q"})
        with db.transaction() as conn:
            return respond(readmodels.forge_items(conn, actor_for(conn, request), project_id, cursor, limit, q=q, config=config))

    @app.get("/api/v3/discussions")
    def discussions(request: Request, project_id: UUID, cursor: str | None = None, limit: int = Query(50, ge=1, le=100), assignment_id: UUID | None = None, q: str = ""):
        from .record_queries import validate_query
        validate_query(request, {"project_id", "cursor", "limit", "assignment_id", "q"})
        with db.transaction() as conn:
            return respond(readmodels.discussions(conn, actor_for(conn, request), project_id, cursor, limit, assignment_id, q=q, config=config))

    @app.post("/api/v3/discussions")
    async def register_discussion(request: Request):
        from .communications import DiscussionRegistration, register_discussion as register
        raw = await request.json()
        data = DiscussionRegistration.model_validate(raw)
        return await project_mutation(request, "register_discussion", raw, "project", data.project_id,
            lambda conn, actor: register(conn, actor, data))

    @app.post("/api/v3/discussion-subjects")
    async def link_discussion_subject(request: Request):
        from .communications import DiscussionSubjectLink, link_subject
        raw = await request.json()
        data = DiscussionSubjectLink.model_validate(raw)
        return await project_mutation(request, "link_discussion_subject", raw, "discussion", data.discussion_id,
            lambda conn, actor: link_subject(conn, actor, data))

    @app.get("/api/v3/discussions/{identifier}/messages")
    def discussion_messages(identifier: UUID, request: Request, cursor: str | None = None,
                            limit: int = Query(20, ge=1, le=100), assignment_id: UUID | None = None,
                            unread_only: bool = False):
        from .record_queries import validate_query
        validate_query(request, {"cursor", "limit", "assignment_id", "unread_only"})
        with db.transaction() as conn:
            return respond(readmodels.discussion_messages(conn, actor_for(conn, request), identifier, cursor, limit,
                assignment_id, unread_only=unread_only))

    def remote_discussion_read(identifier, request, *, search, q, topic, before, limit):
        from urllib.parse import quote
        from .communications import discussion_channel
        from .connectors import ConnectorFailure, ConnectorManager, SecretResolver
        from .integration_views import public_url
        with db.transaction() as conn:
            actor = actor_for(conn, request)
            discussion, integration = discussion_channel(conn, actor, identifier)
        manager = connectors or ConnectorManager(db, service, SecretResolver(config.state_root))
        remote = None
        try:
            remote = manager.remote_client(integration)
            result = (remote.search_messages(discussion["channel_remote_id"], q=q, topic=topic, before=before, limit=limit)
                      if search else remote.topics(discussion["channel_remote_id"], q=q, before=before, limit=limit))
        except ConnectorFailure as error:
            raise DomainError("discussion_search_unavailable", "Zulip discovery is temporarily unavailable", 503,
                              reason=error.code) from None
        finally:
            if remote is not None:
                remote.close()
        base = public_url(integration, config)
        for item in result["items"]:
            topic_name = item["topic"] if search else item["name"]
            item["url"] = (base + "/#narrow/stream/" + quote(discussion["channel_remote_id"], safe="") +
                "/topic/" + quote(topic_name, safe="") + ("/near/" + item["remote_id"] if search else "")) if base else None
        result["source_discussion_id"] = identifier
        result["channel_remote_id"] = discussion["channel_remote_id"]
        result["project_id"] = discussion["project_id"]
        return private_read(request, actor, result)

    @app.get("/api/v3/discussions/{identifier}/topics")
    def discussion_topics(identifier: UUID, request: Request, q: str = Query("", max_length=200),
                          before: int | None = Query(None, ge=1), limit: int = Query(20, ge=1, le=50)):
        from .record_queries import validate_query
        validate_query(request, {"q", "before", "limit"})
        return remote_discussion_read(identifier, request, search=False, q=q, topic=None, before=before, limit=limit)

    @app.get("/api/v3/discussions/{identifier}/search")
    def discussion_search(identifier: UUID, request: Request, q: str = Query(min_length=1, max_length=200),
                          topic: str | None = Query(None, max_length=60), before: int | None = Query(None, ge=1),
                          limit: int = Query(20, ge=1, le=50)):
        from .record_queries import validate_query
        validate_query(request, {"q", "topic", "before", "limit"})
        return remote_discussion_read(identifier, request, search=True, q=q, topic=topic, before=before, limit=limit)

    @app.get("/api/v3/assignments/{identifier}/activity")
    def assignment_activity(identifier: UUID, request: Request, cursor: str | None = None,
                            limit: int = Query(50, ge=1, le=100)):
        with db.transaction() as conn:
            return respond(readmodels.assignment_activity(conn, actor_for(conn, request), identifier, cursor, limit, store=service.store))

    @app.get("/api/v3/projects/{identifier}/integrations")
    def project_integrations(identifier: UUID, request: Request):
        from .integration_views import project_integrations as present_integrations
        with db.transaction() as conn:
            return respond(present_integrations(conn, actor_for(conn, request), identifier, config))

    @app.get("/api/v3/search")
    def search(request: Request, repository_id: UUID, commit: str, q: str,
               mode: str = "text", subdir: str = "", limit: int = Query(20, ge=1, le=100)):
        with db.transaction() as conn:
            actor = actor_for(conn, request)
            repository = get(conn, "repository", repository_id)
            require_project(conn, actor, repository["project_id"])
            integration = get(conn, "integration", repository["integration_id"])
        if search_manager is None:
            raise DomainError("search_disabled", "Search is disabled for this installation", 503)
        from .search import SourceSpec
        if not repository["remote_path"] or ".." in repository["remote_path"].split("/"):
            raise DomainError("invalid_source", "Repository has no valid configured Forge path", 422)
        source = SourceSpec(project_id=str(repository["project_id"]), source_id=str(repository_id),
            url=integration["endpoint"].rstrip("/") + "/" + repository["remote_path"].strip("/") + ".git",
            commit=commit, subdir=subdir)
        try:
            result = search_manager.search(source, q, mode=mode, limit=limit)
        except ValueError as error:
            raise DomainError("invalid_search", str(error), 422) from None
        return JSONResponse(json_value(result), status_code=202 if result["status"] == "preparing" else 200,
                            headers={"Cache-Control": "private, no-cache"})

    @app.get("/api/v3/resources")
    def resources(request: Request):
        with db.transaction() as conn:
            return respond(readmodels.resources(conn, actor_for(conn, request), config))

    @app.get("/api/v3/settings")
    def settings(request: Request):
        with db.transaction() as conn:
            require_admin(conn, actor_for(conn, request))
        return respond({"revision": 1, "configuration": config.redacted()})

    @app.get("/api/v3/references/{identifier}/bibtex")
    def reference_bibtex(identifier: UUID, request: Request):
        with db.transaction() as conn:
            actor = actor_for(conn, request)
            reference = get(conn, "reference", identifier)
            require_project(conn, actor, reference["project_id"])
            etag = '"' + hashlib.sha256(f"bibtex-v1:{actor.id}:{identifier}:{reference['revision']}".encode()).hexdigest() + '"'
            headers = {"ETag": etag, "Cache-Control": "private, no-cache", "Vary": "Cookie, Authorization",
                       "X-Horizon-Reference-Revision": str(reference["revision"])}
            if request.headers.get("if-none-match") == etag:
                return Response(status_code=304, headers=headers)
        from .references import bibtex
        return Response(bibtex([reference]), media_type="application/x-bibtex", headers=headers)

    @app.get("/api/v3/references")
    def references(request: Request, project_id: UUID, q: str = "", cite_key: str | None = None,
                   doi: str | None = None, cursor: str | None = None, limit: int = Query(50, ge=1, le=100)):
        with db.transaction() as conn:
            actor = actor_for(conn, request)
            require_project(conn, actor, project_id)
            table = tables["reference"]
            query = select(table).where(table.c.project_id == project_id)
            if cite_key:
                query = query.where(table.c.cite_key == cite_key)
            if doi:
                from .reference_identifiers import normalize_doi
                try:
                    normalized = normalize_doi(doi)
                except ValueError as error:
                    raise DomainError("invalid_identifier", str(error), 422) from None
                from .references import identifier_expression
                query = query.where(identifier_expression(table, "doi") == normalized)
            if q:
                query = query.where(table.c.title.icontains(q, autoescape=True) | table.c.cite_key.icontains(q, autoescape=True))
            return private_read(request, actor, readmodels.page(conn, query, table, cursor, limit))

    @app.post("/api/v3/reference-usages")
    async def cite_reference(request: Request):
        from .references import ReferenceUsage, record_usage
        raw = await request.json()
        data = ReferenceUsage.model_validate(raw)
        return await project_mutation(request, "cite_reference", raw, "reference", data.reference_id,
            lambda conn, actor: record_usage(conn, actor, data))

    @app.post("/api/v3/artifacts")
    async def upload_blob(request: Request):
        from .content import BlobUpload, upload
        raw = await request.json()
        data = BlobUpload.model_validate(raw)
        return await project_mutation(request, "upload_blob", raw, "project", data.project_id,
            lambda conn, actor: upload(conn, actor, data, service))

    @app.get("/api/v3/artifacts/{identifier}/content")
    def blob_content(identifier: UUID, request: Request):
        with db.transaction() as conn:
            actor = actor_for(conn, request)
            artifact = get(conn, "artifact", identifier)
            require_project(conn, actor, artifact["project_id"])
            if artifact["kind"] != "blob":
                raise DomainError("not_blob", "This artifact has no stored blob content", 422)
            content = artifact["content"]
        body = store.read(content["sha256"], content["size_bytes"])
        etag = '"' + hashlib.sha256(f"{actor.id}:{content['sha256']}".encode()).hexdigest() + '"'
        headers = {"ETag": etag, "Cache-Control": "private, no-cache", "Vary": "Cookie, Authorization",
                   "Content-Disposition": f'attachment; filename="{content["sha256"]}"',
                   "X-Content-SHA256": content["sha256"]}
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        return Response(body, media_type=content["media_type"], headers=headers)

    @app.get("/api/v3/worker/executions/{identifier}/skill-bundle")
    def worker_skill_bundle(identifier: UUID, request: Request):
        with db.transaction() as conn:
            actor = actor_for(conn, request)
            execution = get(conn, "execution", identifier)
            require_host(conn, actor, execution["host_id"])
            artifact = get(conn, "artifact", execution["skill_bundle_artifact_id"])
        return Response(store.read(artifact["content"]["sha256"], artifact["content"]["size_bytes"]),
                        media_type="application/json")

    @app.get("/api/v3/worker/executions/{identifier}/artifacts/{artifact_id}/content")
    def worker_artifact(identifier: UUID, artifact_id: UUID, request: Request):
        with db.transaction() as conn:
            actor = actor_for(conn, request)
            execution = get(conn, "execution", identifier)
            require_host(conn, actor, execution["host_id"])
            link = tables["assignment_artifact"]
            if not conn.execute(select(link).where(link.c.assignment_id == execution["assignment_id"],
                    link.c.artifact_id == artifact_id)).first():
                raise DomainError("scope_mismatch", "Artifact is not assigned to this execution", 403)
            artifact = get(conn, "artifact", artifact_id)
            if artifact["kind"] != "blob":
                raise DomainError("not_blob", "Artifact is not a blob", 422)
        return Response(store.read(artifact["content"]["sha256"], artifact["content"]["size_bytes"]),
                        media_type="application/json")

    @app.post("/api/v3/commands")
    async def command(request: Request):
        from .command_args import COMMAND_ARGS
        raw = await request.json()
        data = Command.model_validate(raw)
        if data.operation in COMMAND_ARGS:
            COMMAND_ARGS[data.operation].model_validate(data.args)
        def work():
            with db.transaction() as conn:
                transaction_lock(conn)
                actor = actor_for(conn, request)
                # Replayed commands still require access to their original scope.
                kind = COMMAND_TARGETS.get(data.operation)
                if kind is None:
                    raise DomainError("unknown_command", "Unsupported command", 422)
                project_id = project_of(conn, kind, data.target_id)
                require_project(conn, actor, project_id, "maintainer" if kind == "run" else "worker")
                return idempotent(conn, actor, request, "command", raw, project_id, lambda: {
                    "id": request.headers.get("idempotency-key"), "status": "completed",
                    "result": execute(conn, actor, data, service, scheduler)})
        return respond(await asyncio.to_thread(work))

    @app.post("/api/v3/reviewer-invocations")
    async def prepare_reviewer(request: Request):
        from .reviewer_invocations import ReviewerPrepare, prepare
        raw = await request.json()
        data = ReviewerPrepare.model_validate(raw)
        await asyncio.to_thread(provision_for_request, request, data.parent_request_id)
        return await project_mutation(request, "prepare_reviewer", raw, "provider_request", data.parent_request_id,
            lambda conn, actor: prepare(conn, actor, data, service), role="maintainer")

    @app.post("/api/v3/reviewer-assignments")
    async def prepare_reviewer_assignment(request: Request):
        from .reviewer_invocations import ReviewerPrepare, prepare_assignment
        raw = await request.json()
        data = ReviewerPrepare.model_validate(raw)
        await asyncio.to_thread(provision_for_request, request, data.parent_request_id)
        return await project_mutation(request, "prepare_reviewer_assignment", raw, "provider_request", data.parent_request_id,
            lambda conn, who: prepare_assignment(conn, who, data, service), role="maintainer")

    @app.get("/api/v3/reviewer-invocations/{identifier}")
    def read_reviewer(identifier: UUID, request: Request):
        from .reviewer_invocations import read
        with db.transaction() as conn:
            return respond(read(conn, actor_for(conn, request), identifier, service))

    @app.post("/api/v3/reviewer-invocations/{identifier}/report")
    async def reviewer_report(identifier: UUID, request: Request):
        from .reviewer_invocations import ReviewerReport, report
        raw = await request.json()
        data = ReviewerReport.model_validate(raw)
        return await project_mutation(request, "reviewer_report", {"id": str(identifier), **raw},
            "provider_request", identifier, lambda conn, actor: report(conn, actor, identifier, data, service,
                request.headers["idempotency-key"]))

    @app.get("/api/v3/executions/{identifier}/reviewer-accounts")
    def reviewer_accounts(identifier: UUID, request: Request):
        from .reviewer_accounts import credentials
        with db.transaction() as conn:
            result = credentials(conn, actor_for(conn, request), identifier, service)
        return JSONResponse(json_value(result), headers={"Cache-Control": "private, no-store", "Vary": "Authorization"})

    @app.post("/api/v3/reviewer-invocations/{identifier}/attach")
    async def attach_reviewer(identifier: UUID, request: Request):
        from .reviewer_invocations import ReviewerAttach, attach
        raw = await request.json()
        data = ReviewerAttach.model_validate(raw)
        return await project_mutation(request, "attach_reviewer", {"id": str(identifier), **raw},
            "provider_request", identifier, lambda conn, actor: attach(conn, actor, identifier, data), role="maintainer")

    @app.post("/api/v3/reviewer-invocations/{identifier}/cancel")
    async def cancel_reviewer(identifier: UUID, request: Request):
        from .reviewer_invocations import ReviewerCancel, cancel
        raw = await request.json()
        data = ReviewerCancel.model_validate(raw)
        return await project_mutation(request, "cancel_reviewer", {"id": str(identifier), **raw},
            "provider_request", identifier, lambda conn, actor: cancel(conn, actor, identifier, data, service=service), role="maintainer")

    @app.get("/api/v3/operations/{key}")
    def operation(key: str, request: Request, operation: str = "command"):
        from .operation_receipts import current_receipt
        with db.transaction() as conn:
            actor = actor_for(conn, request)
            table = tables["api_request"]
            row = conn.execute(receipt_query(conn, actor, operation, key)).mappings().first()
            if not row:
                raise DomainError("not_found", "No committed command with this key is visible", 404)
            if row["project_id"]:
                require_project(conn, actor, row["project_id"])
            else:
                require_admin(conn, actor)
            if row.get("response") is not None:
                original = row["response"]
            elif row["response_artifact_id"]:
                artifact = get(conn, "artifact", row["response_artifact_id"])
                require_project(conn, actor, artifact["project_id"])
                original = json.loads(store.read(artifact["content"]["sha256"]))
            else:
                original = {"id": key, "status": row["status"]}
            return JSONResponse(json_value(current_receipt(conn, actor, row, original)),
                headers={"Cache-Control": "private, no-store", "Vary": "Cookie, Authorization"})

    @app.post("/api/v3/records/{kind}")
    async def create_record(kind: str, request: Request):
        if kind not in models.CREATE_MODELS:
            raise DomainError("unknown_record", "Unknown record kind", 404)
        raw = await request.json()
        data = models.CREATE_MODELS[kind].model_validate(raw)
        def work():
            with db.transaction() as conn:
                transaction_lock(conn)
                actor = actor_for(conn, request)
                project_id = getattr(data, "project_id", None)
                for parent in ("run", "mission", "assignment"):
                    identifier = getattr(data, f"{parent}_id", None)
                    if identifier:
                        project_id = project_of(conn, parent, identifier)
                        break
                if project_id: require_project(conn, actor, project_id, "worker")
                else: require_admin(conn, actor)
                def perform():
                    if kind == "project": return service.project(conn, actor, data)
                    if kind == "mission": return service.mission(conn, actor, data)
                    if kind == "run": return scheduler.run(conn, actor, data)
                    if kind == "assignment": return service.assignment(conn, actor, data)
                    if kind == "obligation": return service.obligation(conn, actor, data)
                    if kind == "subscription":
                        from .communications import subscribe
                        return subscribe(conn, actor, data, service)
                    if kind == "automation":
                        require_project(conn, actor, project_id, "maintainer")
                        run = get(conn, "run", data.run_id)
                        mission = require_mission_authority(conn, actor, data.mission_id, role="worker")
                        if project_of(conn, "run", run["id"]) != project_id or mission["project_id"] != project_id:
                            raise DomainError("scope_mismatch", "Automation run and mission must belong to the same project", 422)
                        if actor.kind == "agent" and live_execution(conn, actor)["run_id"] != run["id"]:
                            raise DomainError("forbidden", "The automation belongs to another run", 403)
                        if run["status"] != "active":
                            raise DomainError("run_not_active", "Automations require an active run", 409)
                        if mission["status"] != "open":
                            raise DomainError("mission_closed", "Automations require an open mission", 409)
                        if not contains_mission(conn, run["mission_id"], mission["id"]):
                            raise DomainError("scope_mismatch", "Automation mission must belong to the run's mission tree", 422)
                        service.validate_condition(conn, project_id, data.start_condition.model_dump(mode="json") if data.start_condition else None)
                        row = create(conn, kind, **{**data.model_dump(), "start_condition": data.start_condition.model_dump(mode="json") if data.start_condition else None})
                        scheduler.replenish(conn, actor, row)
                        return row
                    return create_catalog(conn, actor, kind, raw)
                return idempotent(conn, actor, request, f"create_{kind}", raw, project_id, perform)
        result = await asyncio.to_thread(work)
        if kind in {"reviewer_descriptor", "review_policy", "repository"}:
            result = await asyncio.to_thread(provision_catalog_result, kind, result)
        return respond(result)

    readable = set(models.CREATE_MODELS) | {"execution", "provider_thread", "provider_request", "activity", "artifact",
                "publication", "forge_item", "forge_review", "review_gate", "discussion", "message", "notification", "verification", "outbox_operation"}
    readable.discard("host_harness")

    @app.get("/api/v3/records/{kind}")
    def list_records(kind: str, request: Request, project_id: UUID | None = None, assignment_id: UUID | None = None,
                     run_id: UUID | None = None, repository_id: UUID | None = None, mission_id: UUID | None = None,
                     discussion_id: UUID | None = None, forge_item_id: UUID | None = None, status: str | None = None,
                     cursor: str | None = None, limit: int = Query(50, ge=1, le=100)):
        from .record_queries import scope_expression, supported_filters, validate_query
        if kind not in readable:
            raise DomainError("unknown_record", "This record is not publicly readable", 404)
        validate_query(request, supported_filters(kind))
        with db.transaction() as conn:
            actor = actor_for(conn, request)
            table = tables[kind]
            scopes = {"assignment": assignment_id, "run": run_id, "repository": repository_id,
                      "mission": mission_id, "discussion": discussion_id, "forge_item": forge_item_id}
            for scope, identifier in scopes.items():
                if identifier:
                    derived = project_of(conn, scope, identifier)
                    require_project(conn, actor, derived)
                    if project_id and project_id != derived:
                        raise DomainError("scope_mismatch", "Filters belong to different projects", 422)
                    project_id = derived
            if project_id:
                require_project(conn, actor, project_id)
                query = scoped_query(kind, project_id)
            else:
                require_admin(conn, actor)
                query = select(table)
            for scope, identifier in scopes.items():
                if identifier:
                    query = query.where(scope_expression(kind, scope, identifier))
            if status is not None:
                query = query.where(table.c.status == status)
            return private_read(request, actor, readmodels.page(conn, query, table, cursor, limit))

    @app.get("/api/v3/records/{kind}/{identifier}")
    def record_detail(kind: str, identifier: UUID, request: Request):
        if kind not in readable:
            raise DomainError("unknown_record", "This record is not publicly readable", 404)
        with db.transaction() as conn:
            actor = actor_for(conn, request)
            project_id = project_of(conn, kind, identifier)
            if project_id: require_project(conn, actor, project_id)
            else: require_admin(conn, actor)
            row = get(conn, kind, identifier)
            for field, (join_name, own_key, other_key) in models.RELATIONS.get(kind, {}).items():
                link = tables[join_name]
                row[field] = list(conn.execute(select(link.c[other_key]).where(link.c[own_key] == identifier)).scalars())
            return private_read(request, actor, row)

    @app.patch("/api/v3/records/{kind}/{identifier}")
    async def patch_catalog(kind: str, identifier: UUID, request: Request):
        if kind not in UPDATE_FIELDS:
            raise DomainError("unknown_record", "Use the record's lifecycle command", 404)
        raw = await request.json()
        data = CatalogUpdate.model_validate(raw)
        def work():
            with db.transaction() as conn:
                transaction_lock(conn)
                actor = actor_for(conn, request)
                project_id = project_of(conn, kind, identifier)
                if project_id:
                    require_project(conn, actor, project_id, "worker")
                else:
                    require_admin(conn, actor)
                return idempotent(conn, actor, request, f"update_{kind}", {"id": str(identifier), **raw}, project_id,
                    lambda: update_catalog(conn, actor, kind, identifier, data))
        result = await asyncio.to_thread(work)
        if kind in {"reviewer_descriptor", "review_policy", "repository"}:
            result = await asyncio.to_thread(provision_catalog_result, kind, result)
        return respond(result)

    @app.get("/api/v3/hosts/{host_id}/harnesses")
    def host_harnesses(host_id: UUID, request: Request):
        with db.transaction() as conn:
            require_admin(conn, actor_for(conn, request))
            host = get(conn, "host", host_id)
            table = tables["host_harness"]
            rows = conn.execute(select(table).where(table.c.host_id == host_id)).mappings().all()
            return respond({"host_revision": host["revision"], "items": [
                {"harness_id": row["harness_id"], "execution_slots": row["execution_slots"],
                 "max_parallel_subagents": row["max_parallel_subagents"], "enabled": row["enabled"],
                 "credential_configured": bool(row["credential_ref"])} for row in rows]})

    @app.patch("/api/v3/hosts/{host_id}/harnesses/{harness_id}")
    async def patch_host_harness(host_id: UUID, harness_id: UUID, request: Request):
        raw = await request.json()
        data = CatalogUpdate.model_validate(raw)
        def work():
            with db.transaction() as conn:
                transaction_lock(conn)
                actor = actor_for(conn, request)
                require_admin(conn, actor)
                return idempotent(conn, actor, request, "configure_host_harness",
                    {"host_id": str(host_id), "harness_id": str(harness_id), **raw}, None,
                    lambda: configure_host_harness(conn, actor, host_id, harness_id, data))
        return respond(await asyncio.to_thread(work))

    @app.patch("/api/v3/missions/{identifier}")
    async def update_mission(identifier: UUID, request: Request):
        raw = await request.json()
        data = models.MissionUpdate.model_validate(raw)
        def work():
            with db.transaction() as conn:
                transaction_lock(conn)
                actor = actor_for(conn, request)
                project_id = project_of(conn, "mission", identifier)
                require_project(conn, actor, project_id, "worker")
                return idempotent(conn, actor, request, "update_mission", {"id": str(identifier), **raw}, project_id,
                    lambda: service.update_mission(conn, actor, identifier, data))
        return respond(await asyncio.to_thread(work))

    @app.post("/api/v3/obligations/{identifier}/resolve")
    async def resolve_obligation(identifier: UUID, request: Request):
        raw = await request.json()
        data = models.ObligationResolve.model_validate(raw)
        def work():
            with db.transaction() as conn:
                transaction_lock(conn)
                actor = actor_for(conn, request)
                project_id = project_of(conn, "obligation", identifier)
                require_project(conn, actor, project_id, "worker")
                return idempotent(conn, actor, request, "resolve_obligation", {"id": str(identifier), **raw}, project_id,
                    lambda: service.resolve_obligation(conn, actor, identifier, data))
        return respond(await asyncio.to_thread(work))

    @app.get("/api/v3/worker/hosts/{host_id}/unconfirmed-executions")
    def unconfirmed_executions(host_id: UUID, request: Request):
        with db.transaction() as conn:
            require_host(conn, actor_for(conn, request), host_id)
            execution = tables["execution"]
            rows = conn.execute(select(execution.c.id, execution.c.number, execution.c.status,
                execution.c.assignment_id, execution.c.workspace_id, execution.c.started_at,
                execution.c.lease_expires_at).where(execution.c.host_id == host_id,
                    execution.c.stop_confirmed_at.is_(None)).order_by(execution.c.created_at,
                        execution.c.id).limit(101)).mappings().all()
            return respond({"executions": [dict(row) for row in rows[:100]], "has_more": len(rows) > 100})

    @app.post("/api/v3/worker/hosts/{host_id}/heartbeat")
    async def host_heartbeat(host_id: UUID, request: Request):
        data = models.HostHeartbeat.model_validate(await request.json())
        def work():
            with db.transaction() as conn:
                require_host(conn, actor_for(conn, request), host_id)
                row = change(conn, "host", host_id, heartbeat_at=func.clock_timestamp(),
                             health=data.health.model_dump(mode="json", exclude_unset=True))
                return {key: row[key] for key in ("heartbeat_at", "health")} | {"host_id": host_id}
        return respond(await asyncio.to_thread(work))

    @app.post("/api/v3/worker/claim")
    async def claim(request: Request):
        raw = await request.json()
        from pydantic import TypeAdapter
        class Claim(models.Contract):
            host_id: UUID
            harness_ids: list[UUID]
        data = Claim.model_validate(raw)
        def work():
            with db.transaction() as conn:
                transaction_lock(conn)
                actor = actor_for(conn, request)
                require_host(conn, actor, data.host_id)
                result = idempotent(conn, actor, request, "worker_claim", raw, None,
                    lambda: {"execution": scheduler.claim(conn, actor, data.host_id, data.harness_ids)})
                if result["execution"]:
                    grant = dict(result["execution"])
                    current = get(conn, "execution", grant["execution_id"])
                    require_host(conn, actor, current["host_id"])
                    now = conn.execute(select(func.clock_timestamp())).scalar_one()
                    remaining = (current["lease_expires_at"] - now).total_seconds()
                    valid = current["status"] in ("starting", "running") and current["number"] == grant["epoch"]
                    # Receipts preserve the admitted identity, never renew its lease.
                    grant["lease_seconds"] = max(0, min(grant["lease_seconds"], remaining)) if valid else 0
                    result = {**result, "execution": grant}
                return result
        return respond(await asyncio.to_thread(work))

    @app.post("/api/v3/messages/read")
    async def message_receipts(request: Request):
        class Receipts(models.Contract):
            assignment_id: UUID
            messages: list[models.MessageRevision]
        data = Receipts.model_validate(await request.json())
        from .communications import read_messages
        return await project_mutation(request, "read_messages", data.model_dump(mode="json"), "assignment", data.assignment_id,
            lambda conn, actor: read_messages(conn, actor, data.assignment_id,
                [item.model_dump(mode="json") for item in data.messages], service))

    @app.post("/api/v3/discussions/{identifier}/reply")
    async def reply(identifier: UUID, request: Request):
        class Reply(models.Contract):
            assignment_id: UUID | None = None
            body: models.Text
            read_messages: list[models.MessageRevision] = []
            urgent: bool = False
        data = Reply.model_validate(await request.json())
        from .communications import queue_reply
        return await project_mutation(request, "discussion_reply", {"id": str(identifier), **data.model_dump(mode="json")},
            "discussion", identifier, lambda conn, actor: queue_reply(conn, actor, service, discussion_id=identifier,
                assignment_id=data.assignment_id, body=data.body,
                read_revisions=[value.model_dump(mode="json") for value in data.read_messages], urgent=data.urgent,
                key=request.headers.get("idempotency-key")))

    @app.get("/api/v3/notifications/{identifier}")
    def notification_detail(identifier: UUID, request: Request):
        with db.transaction() as conn:
            actor = actor_for(conn, request)
            notice = get(conn, "notification", identifier)
            project_id = project_of(conn, "assignment", notice["assignment_id"])
            require_project(conn, actor, project_id)
            event = get(conn, "event", notice["event_id"])
            if event["project_id"] != project_id:
                raise DomainError("scope_mismatch", "Notification source belongs to another project", 422)
            return private_read(request, actor, {**notice, "event": event})

    @app.get("/api/v3/assignments/{identifier}/control-notices")
    def pending_control_notices(identifier: UUID, request: Request):
        from .notifications import control_summary
        with db.transaction() as conn:
            actor = actor_for(conn, request)
            require_project(conn, actor, project_of(conn, "assignment", identifier))
            return respond(control_summary(conn, identifier))

    @app.post("/api/v3/assignments/{identifier}/control-notices")
    async def send_control_notice(identifier: UUID, request: Request):
        from .notifications import OperatorNotice, operator_notice, require_operator
        data = OperatorNotice.model_validate(await request.json())
        def work():
            with db.transaction() as conn:
                transaction_lock(conn)
                actor = actor_for(conn, request)
                project_id = require_operator(conn, actor, identifier)
                return idempotent(conn, actor, request, "operator_notice",
                    {"assignment_id": str(identifier), **data.model_dump(mode="json")}, project_id,
                    lambda: operator_notice(conn, actor, identifier, data))
        return respond(await asyncio.to_thread(work))

    @app.post("/api/v3/notifications/{identifier}/disposition")
    async def disposition(identifier: UUID, request: Request):
        from typing import Literal
        class Disposition(models.Contract):
            expected_revision: models.Positive
            disposition: Literal["handled", "dismissed"]
            note: models.Text
            obligation_id: UUID | None = None
        data = Disposition.model_validate(await request.json())
        def perform(conn, actor):
            notice = get(conn, "notification", identifier)
            service.require_ledger_owner(conn, actor, notice["assignment_id"])
            if data.obligation_id and get(conn, "obligation", data.obligation_id)["assignment_id"] != notice["assignment_id"]:
                raise DomainError("scope_mismatch", "Notification follow-up must belong to its assignment", 422)
            return change(conn, "notification", identifier, data.expected_revision, disposition=data.disposition,
                          disposition_note=data.note, obligation_id=data.obligation_id)
        return await project_mutation(request, "notification_disposition", {"id": str(identifier), **data.model_dump(mode="json")},
                                      "notification", identifier, perform)

    @app.post("/api/v3/forge/review")
    async def review(request: Request):
        raw = await request.json()
        data = models.ForgeReview.model_validate(raw)
        from .reviews import queue_review
        return await project_mutation(request, "forge_review", raw, "forge_item", data.forge_item_id,
            lambda conn, actor: queue_review(conn, actor, service, scheduler, raw, request.headers.get("idempotency-key")), role="maintainer")

    @app.get("/api/v3/repositories/{identifier}/head")
    def repository_head(identifier: UUID, request: Request, branch: str | None = Query(None, min_length=1, max_length=1024)):
        from .forge_inspection import repository_head as read_head
        from .record_queries import validate_query
        validate_query(request, {"branch"})
        return JSONResponse(json_value(read_head(db, config, lambda conn: actor_for(conn, request), identifier, branch)),
                            headers={"Cache-Control": "no-store", "Vary": "Authorization, Cookie"})

    @app.get("/api/v3/repositories/{identifier}/file")
    def forge_file(identifier: UUID, request: Request, commit_oid: str = Query(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"),
                   path: str = Query(min_length=1, max_length=2000)):
        from .forge_inspection import inspect
        from .record_queries import validate_query
        validate_query(request, {"commit_oid", "path"})
        result = inspect(db, config, lambda conn: actor_for(conn, request), repository_id=identifier,
                         commit_oid=commit_oid, file_path=path)
        return JSONResponse(json_value(result), headers={"Cache-Control": "private, no-store", "Vary": "Authorization, Cookie"})

    @app.get("/api/v3/forge-items/{identifier}/inspect")
    def forge_inspect(identifier: UUID, request: Request,
                      view: Literal["files", "diff", "comments", "reviews", "review_comments"],
                      expected_head_oid: str | None = Query(None, pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"),
                      page: int = Query(1, ge=1, le=10000), limit: int = Query(50, ge=1, le=100),
                      review_id: int | None = Query(None, ge=1)):
        from .forge_inspection import inspect
        from .record_queries import validate_query
        validate_query(request, {"view", "expected_head_oid", "page", "limit", "review_id"})
        if view == "review_comments" and review_id is None:
            raise DomainError("invalid_query", "review_comments requires review_id", 422)
        result = inspect(db, config, lambda conn: actor_for(conn, request), forge_item_id=identifier,
                         view=view, expected_head_oid=expected_head_oid, page=page, limit=limit, review_id=review_id)
        return JSONResponse(json_value(result), headers={"Cache-Control": "private, no-store", "Vary": "Authorization, Cookie"})

    @app.post("/api/v3/forge/create")
    async def forge_create(request: Request):
        data = models.ForgeCreate.model_validate(await request.json())
        def perform(conn, actor):
            from .auth import live_execution
            from .records import same_project
            repository = get(conn, "repository", data.repository_id)
            run = same_project(conn, "run", data.origin_run_id, repository["project_id"])
            if actor.kind == "agent" and live_execution(conn, actor)["run_id"] != run["id"]:
                raise DomainError("scope_mismatch", "Forge origin must be the submitting agent's run", 422)
            if run["phase"]["kind"] != data.review_phase:
                raise DomainError("phase_mismatch", "Forge routing phase must match its origin run", 422)
            payload = data.model_dump(mode="json", exclude_none=True)
            if data.kind == "pull_request":
                payload["base"] = data.base or repository["default_branch"]
                if payload["base"] != repository["default_branch"] and not data.stack_reason:
                    raise DomainError("stack_reason_required", "Use the repository default branch, or explain the stack dependency and its integration path", 422)
                if data.stack_reason:
                    payload["body"] += "\n\nStack dependency: " + data.stack_reason
            return create(conn, "outbox_operation", project_id=repository["project_id"], actor_principal_id=actor.id,
                          kind="forge_create", schema_version=1, idempotency_key=request.headers.get("idempotency-key"),
                          payload=payload)
        return await project_mutation(request, "forge_create", data.model_dump(mode="json"), "repository", data.repository_id, perform)

    @app.post("/api/v3/forge/change")
    async def forge_change(request: Request):
        from .forge_changes import queue
        data = models.ForgeChange.model_validate(await request.json())
        return await project_mutation(request, "forge_change", data.model_dump(mode="json"), "repository", data.repository_id,
            lambda conn, actor: queue(conn, actor, data, request.headers.get("idempotency-key")))

    @app.post("/api/v3/forge/edit")
    async def forge_edit(request: Request):
        from .reviews import queue_edit
        data = models.ForgeEdit.model_validate(await request.json())
        return await project_mutation(request, "forge_edit", data.model_dump(mode="json"), "forge_item", data.forge_item_id,
            lambda conn, actor: queue_edit(conn, actor, data, request.headers.get("idempotency-key")), role="maintainer")

    @app.post("/api/v3/forge/comment")
    async def forge_comment(request: Request):
        from .reviews import queue_comment
        data = models.ForgeComment.model_validate(await request.json())
        return await project_mutation(request, "forge_comment", data.model_dump(mode="json"), "forge_item", data.forge_item_id,
            lambda conn, actor: queue_comment(conn, actor, data, scheduler, request.headers.get("idempotency-key")))

    @app.post("/api/v3/forge/merge")
    async def merge(request: Request):
        raw = await request.json()
        data = models.ForgeMerge.model_validate(raw)
        from .reviews import queue_merge
        return await project_mutation(request, "forge_merge", raw, "forge_item", data.forge_item_id,
            lambda conn, actor: queue_merge(conn, actor, raw, request.headers.get("idempotency-key")), role="maintainer")

    @app.post("/api/v3/forge/label")
    async def label(request: Request):
        raw = await request.json()
        data = models.ForgeLabel.model_validate(raw)
        from .reviews import queue_label
        return await project_mutation(request, "forge_label", raw, "forge_item", data.forge_item_id,
            lambda conn, actor: queue_label(conn, actor, raw, request.headers.get("idempotency-key")))

    @app.post("/api/v3/forge-items/{identifier}/classify")
    async def classify(identifier: UUID, request: Request):
        class Classification(models.Contract):
            expected_revision: models.Positive
            phase: models.PhaseKind
            note: models.Text
        data = Classification.model_validate(await request.json())
        def perform(conn, actor):
            from .records import emit
            item = get(conn, "forge_item", identifier)
            scheduler.matching_policy(conn, item["repository_id"], data.phase, required=True)
            row = change(conn, "forge_item", identifier, data.expected_revision, review_phase=data.phase)
            emit(conn, actor.id, project_of(conn, "forge_item", identifier), "forge_item", row, ["review_phase"])
            return row
        return await project_mutation(request, "forge_classify", {"id": str(identifier), **data.model_dump(mode="json")},
                                      "forge_item", identifier, perform, role="maintainer")

    @app.post("/api/v3/worker/executions/{identifier}/heartbeat")
    async def heartbeat(identifier: UUID, request: Request):
        raw = await request.json()
        if (set(raw) - {"epoch", "provider_thread_id"} or "epoch" not in raw
                or type(raw["epoch"]) is not int or raw["epoch"] < 1
                or (raw.get("provider_thread_id") is not None and not isinstance(raw["provider_thread_id"], str))):
            raise DomainError("invalid_epoch", "Heartbeat requires a positive epoch", 422)
        def work():
            with db.transaction() as conn:
                transaction_lock(conn)
                if raw.get("provider_thread_id"):
                    execution = get(conn, "execution", identifier)
                    thread = tables["provider_thread"]
                    native = conn.execute(select(thread.c.provider_thread_id).where(
                        thread.c.assignment_id == execution["assignment_id"], thread.c.kind == "primary",
                        thread.c.status.in_(("creating", "available")))).scalar_one_or_none()
                    if native and native != raw["provider_thread_id"]:
                        raise DomainError("context_identity_changed", "Native context differs from the retained context")
                return scheduler.heartbeat(conn, actor_for(conn, request), identifier, raw["epoch"])
        return respond(await asyncio.to_thread(work))

    @app.post("/api/v3/worker/operations")
    async def worker_operation(request: Request):
        raw = await request.json()
        data = WorkerOperation.model_validate(raw)
        def work():
            with db.transaction() as conn:
                transaction_lock(conn)
                actor = actor_for(conn, request)
                execution = get(conn, "execution", data.execution_id)
                from .auth import require_host
                require_host(conn, actor, execution["host_id"])
                return idempotent(conn, actor, request, "worker_operation", raw, project_of(conn, "execution", data.execution_id),
                    lambda: handle_worker(conn, actor, data, service, scheduler))
        return respond(await asyncio.to_thread(work))

    @app.get("/api/v3/events")
    async def events(request: Request, after: int = Query(0, ge=0), project_id: UUID | None = None):
        last = request.headers.get("last-event-id")
        if last and last.isdecimal(): after = max(after, int(last))
        with db.transaction() as conn:
            actor = actor_for(conn, request)
            if project_id: require_project(conn, actor, project_id)
        async def stream():
            cursor = after
            initialized = False
            def read_batch():
                with db.transaction() as conn:
                    current_actor = actor_for(conn, request)
                    # Materialize permitted project IDs to avoid accidentally adding an unscoped FROM.
                    project_ids = [row["id"] for row in conn.execute(readmodels.visible_projects(conn, current_actor)).mappings()]
                    event = tables["event"]
                    reference = tables["object_reference"]
                    floor, head = readmodels.replay_window(conn, config.storage.event_replay_retention_seconds)
                    effective = head if (not initialized and cursor == 0) or cursor > head else max(cursor, floor)
                    query = select(event, reference.c.kind.label("subject_kind")).outerjoin(reference,
                        event.c.subject_id == reference.c.id).where(event.c.sequence > effective)
                    if project_id:
                        require_project(conn, current_actor, project_id)
                        query = query.where(event.c.project_id == project_id)
                    elif is_admin(conn, current_actor):
                        pass
                    else:
                        query = query.where(event.c.project_id.in_(project_ids))
                    return effective, [dict(row) for row in conn.execute(query.order_by(event.c.sequence).limit(200)).mappings()]
            while not stop.is_set() and not await request.is_disconnected():
                try:
                    effective, batch = await asyncio.to_thread(read_batch)
                except DomainError:
                    return
                if cursor != effective or not initialized:
                    cursor = effective
                    yield f"id: {cursor}\nevent: gap\ndata: {json.dumps({'sequence': cursor, 'gap': True, 'resources': []})}\n\n"
                initialized = True
                if batch:
                    for event in batch:
                        cursor = event["sequence"]
                        resources_by_subject = {"project": ["projects"], "run": ["runs", "assignments", "roadmap"],
                            "mission": ["runs", "assignments"], "node": ["roadmap"], "document": ["roadmap"],
                            "roadmap_snapshot": ["roadmap"], "publication": ["changes", "assignments"],
                            "forge_item": ["changes", "assignments", "roadmap"], "review_gate": ["changes", "roadmap"],
                            "discussion": ["discussions"], "message": ["discussions"],
                            "host": ["resources"], "harness": ["resources"], "execution": ["assignments", "resources"]}
                        resources = resources_by_subject.get(event["subject_kind"], ["assignments"])
                        if {'milestone_check', 'milestone_job'} & set(event['payload'].get('changed_fields', [])):
                            resources = ['roadmap']
                        payload = {"sequence": cursor, "project_id": str(event["project_id"]) if event["project_id"] else None,
                                   "resources": resources}
                        yield f"id: {cursor}\nevent: invalidate\ndata: {json.dumps(payload)}\n\n"
                else:
                    yield ": keepalive\n\n"
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=2)
                    except TimeoutError:
                        pass
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    frontend = Path(__file__).parents[1] / "frontend" / "dist"

    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    def dashboard_entry(request: Request):
        navigation_keys = {"tab", "project", "view", "node", "objective", "run", "session", "assignment", "q", "mode", "pool"}
        query = urlencode([(key, value) for key, value in request.query_params.multi_items() if key in navigation_keys])
        return RedirectResponse("/pipeline" + ("?" + query if query else ""), status_code=307)

    @app.get("/pipeline")
    @app.get("/pipeline/{path:path}")
    def dashboard(path: str = ""):
        index = frontend / "index.html"
        if not index.is_file():
            raise DomainError("dashboard_missing", "Install the prebuilt dashboard bundle", 503)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    @app.get("/assets/{path:path}")
    def static_asset(path: str):
        root = (frontend / "assets").resolve()
        asset = (root / path).resolve()
        if not asset.is_relative_to(root) or not asset.is_file():
            raise DomainError("not_found", "Asset was not found", 404)
        return FileResponse(asset, headers={"Cache-Control": "public, max-age=31536000, immutable"})

    return app
