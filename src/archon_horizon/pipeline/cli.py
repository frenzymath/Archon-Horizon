"""Operator and agent commands for the Horizon control plane."""

from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import tempfile
from uuid import UUID

from .errors import DomainError


def parser():
    root = argparse.ArgumentParser(prog="horizon-pipeline")
    root.add_argument("--config", type=Path, help="Explicit pipeline JSON configuration")
    commands = root.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Prepare a new installation without changing any existing one")
    init.add_argument("--state-root", type=Path)
    init.add_argument("--database-url", default=os.environ.get("HORIZON_PIPELINE_DATABASE_URL"))
    init.add_argument("--public-url")
    interaction = init.add_mutually_exclusive_group()
    interaction.add_argument("--interactive", action="store_true", default=None)
    interaction.add_argument("--non-interactive", action="store_false", dest="interactive")
    init.add_argument("--apply", action="store_true", help="Create the reviewed configuration; default only prints the plan")
    commands.add_parser("migrate", help="Explicitly apply pipeline schema migrations to the configured database")
    commands.add_parser("serve", help="Run the API and deterministic watchdog")
    doctor_parser = commands.add_parser("doctor", help="Read-only checks of this configured installation")
    doctor_parser.add_argument("--worker-config", type=Path, help="Also inspect this explicit local worker's prerequisites")
    watchdog = commands.add_parser("watchdog", help="Probe local API liveness and supervise an explicitly selected user service")
    watchdog.add_argument("--restart-service", required=True)
    watchdog.add_argument("--failure-threshold", type=int, default=3)
    watchdog.add_argument("--cooldown-seconds", type=int, default=300)
    worker_watchdog = commands.add_parser("worker-watchdog", help="Supervise local worker progress and restart a stalled daemon")
    worker_watchdog.add_argument("--worker-config", type=Path, required=True)
    worker_watchdog.add_argument("--restart-service", required=True)
    worker_watchdog.add_argument("--failure-threshold", type=int, default=3)
    worker_watchdog.add_argument("--cooldown-seconds", type=int, default=300)
    commands.add_parser("export-config", help="Print redacted operator configuration")
    backup = commands.add_parser("backup", help="Write a consistent PostgreSQL and shared-artifact backup")
    backup.add_argument("--destination", type=Path, required=True)
    verify = commands.add_parser("verify-backup", help="Verify every checksum in an existing backup")
    verify.add_argument("--backup", type=Path, required=True)
    cleanup = commands.add_parser("cleanup", help="Preview eligible owned diagnostics; apply an exact reviewed preview")
    cleanup.add_argument("--apply-preview", type=Path)
    cleanup.add_argument("--database", action="store_true", help="Preview or apply bounded transport-history retention instead of filesystem cleanup")
    for name, help_text in (("bootstrap-project", "Review/apply an atomic project and host setup recipe"),
                            ("launch-run", "Review/launch a run after its workers are ready")):
        recipe = commands.add_parser(name, help=help_text)
        recipe.add_argument("--input", type=Path, required=True)
        recipe.add_argument("--operator", required=True)
        recipe.add_argument("--apply", action="store_true")
    workspaces = commands.add_parser("verify-workspaces", help="Verify existing Git checkouts for one authenticated local worker")
    workspaces.add_argument("--worker-config", type=Path, required=True)
    workspaces.add_argument("--operator", required=True)
    workspaces.add_argument("--apply", action="store_true", help="Mark only successfully verified initial checkouts ready")
    user = commands.add_parser("create-admin", help="Create a local administrator; password is never a command argument")
    user.add_argument("username")
    user.add_argument("--password-stdin", action="store_true")
    hostkey = commands.add_parser("host-key", help="Issue a narrowly scoped daemon credential")
    hostkey.add_argument("host_id")
    hostkey.add_argument("--output", type=Path, required=True)
    worker = commands.add_parser("worker", help="Run a daemon from explicit local worker configuration")
    worker.add_argument("--worker-config", type=Path, required=True)
    reconcile = commands.add_parser("worker-reconcile-claim", help="Resolve a local uncertain claim only after the host has no unconfirmed executions")
    reconcile.add_argument("--worker-config", type=Path, required=True)
    reconcile.add_argument("--note", required=True)
    publications = commands.add_parser("worker-publications", help="Inspect durable worker publications or retry a repaired blocked delivery")
    publications.add_argument("--worker-config", type=Path, required=True)
    publications.add_argument("--retry", metavar="OPERATION_ID")
    publications.add_argument("--note")
    agent = commands.add_parser("agent", help="Use the scoped execution environment")
    actions = agent.add_subparsers(dest="action", required=True)
    context = actions.add_parser("context")
    context.add_argument("--view", choices=("brief", "full", "operations"), default="brief",
                         help="Current assignment, full history, or bounded run operations")
    schema = actions.add_parser("schema", help="Discover request contracts, optionally one section or operation")
    schema.add_argument("--section", help="operations: endpoint bodies; command_args: command arguments; create: record kinds; queries: GET query fields; routes: operation-to-route map")
    schema.add_argument("--name", help="Literal entry name; e.g. --section operations --name prepare_reviewer, or --section command_args --name defer_automation")
    actions.add_parser("pending", help="Inspect durable intents still awaiting an authoritative outcome")
    actions.add_parser("reviewer-accounts", help="Refresh private reviewer credentials and print only their file path")
    actions.add_parser("replay", help="Replay a bounded intent batch under the current execution's authority")
    resolve = actions.add_parser("resolve-intent", help="Record a repaired or delegated request in the central ledger before clearing its local blocker")
    resolve.add_argument("key")
    resolve.add_argument("--note", required=True)
    reference = actions.add_parser("reference", help="Fetch authenticated BibTeX into a bounded workspace cache")
    reference.add_argument("id")
    reference.add_argument("--workspace", type=Path, required=True)
    reference.add_argument("--path", action="store_true", dest="print_path")
    upload = actions.add_parser("upload-file", help="Upload exact file bytes as a journaled project artifact")
    upload.add_argument("path", type=Path)
    upload.add_argument("--project-id", type=UUID, required=True)
    upload.add_argument("--media-type", default="text/plain")
    request = actions.add_parser("request")
    request.add_argument("method", choices=("GET", "POST", "PATCH"))
    request.add_argument("path")
    body = request.add_mutually_exclusive_group()
    body.add_argument("json", nargs="?")
    body.add_argument("--body-file", type=Path, help="Read JSON from a file without shell quoting")
    request.add_argument("--key")
    return root


def database(config):
    from .database import Database
    return Database(config.database_url.get_secret_value(), pool_size=config.database_pool_size,
                    pool_timeout=config.database_pool_timeout_seconds,
                    statement_timeout_ms=config.statement_timeout_seconds * 1000)


def agent_command(args):
    from .client import AgentClient, MAX_REQUEST_BYTES
    required = ("HORIZON_API_URL", "HORIZON_EXECUTION_TOKEN", "HORIZON_EXECUTION_ID", "HORIZON_ASSIGNMENT_ID", "HORIZON_AGENT_STATE")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise ValueError("Agent commands require a dispatched execution: " + ", ".join(missing))
    state = Path(os.environ["HORIZON_AGENT_STATE"])
    replay_seconds = int(os.environ.get("HORIZON_MAX_OFFLINE_REPLAY_SECONDS", str(7 * 86400)))
    if not 1 <= replay_seconds <= 365 * 86400:
        raise ValueError("HORIZON_MAX_OFFLINE_REPLAY_SECONDS must be between 1 and 31536000")
    client = AgentClient(os.environ[required[0]], os.environ[required[1]], os.environ[required[2]], state,
                         max_offline_seconds=replay_seconds, assignment_id=os.environ["HORIZON_ASSIGNMENT_ID"])
    try:
        if args.action == "context":
            path = f"/api/v3/assignments/{os.environ['HORIZON_ASSIGNMENT_ID']}/context"
            result = client.request("GET", path + ("?view=" + args.view if args.view != "brief" else ""))
        elif args.action == "schema":
            from urllib.parse import urlencode
            query = urlencode({key: value for key, value in (('section', args.section), ('name', args.name)) if value})
            result = client.request("GET", "/api/v3/schema" + ("?" + query if query else ""))
        elif args.action == "pending":
            result = client.pending()
        elif args.action == "reviewer-accounts":
            execution_id = os.environ["HORIZON_EXECUTION_ID"]
            configured_path = os.environ.get("HORIZON_REVIEWER_ACCOUNTS_FILE")
            scratch = os.environ.get("TMPDIR")
            if not configured_path and not scratch:
                raise ValueError("Reviewer credentials require HORIZON_REVIEWER_ACCOUNTS_FILE or an explicit TMPDIR")
            target = Path(configured_path) if configured_path else Path(scratch) / "reviewer-accounts.json"
            if not target.is_absolute() or ".." in target.parts or target.is_symlink():
                raise ValueError("Reviewer credential file must be an absolute, non-symlink path")
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            result = client.request("GET", f"/api/v3/executions/{execution_id}/reviewer-accounts")
            if (not isinstance(result, dict) or result.get("execution_id") != execution_id
                    or not isinstance(result.get("accounts"), list)
                    or any(not isinstance(account, dict) or not isinstance(account.get("token"), str)
                           or not account["token"] for account in result["accounts"])):
                raise ValueError("Invalid scoped reviewer credentials response")
            content = json.dumps(result).encode()
            if len(content) > 1024**2:
                raise ValueError("Reviewer credentials exceed the private file size limit")
            fd, temporary = tempfile.mkstemp(prefix=".reviewer-accounts-", dir=target.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            print(str(target))
            return
        elif args.action == "replay":
            result = client.replay_pending()
        elif args.action == "resolve-intent":
            result = client.resolve_intent(args.key, args.note)
        elif args.action == "upload-file":
            import base64
            import re

            if not re.fullmatch(r"[a-zA-Z0-9.+-]+/[a-zA-Z0-9.+-]+", args.media_type):
                raise ValueError("Artifact media type must be a type/subtype without parameters")
            if not args.path.is_file():
                raise ValueError("Artifact upload requires a regular file")
            payload = {"project_id": str(args.project_id), "media_type": args.media_type, "content_base64": ""}
            envelope_size = len(json.dumps(payload, sort_keys=True, separators=(",", ":")))
            # Base64 expands each three source bytes to four request bytes.
            maximum = max(0, ((MAX_REQUEST_BYTES - envelope_size) // 4) * 3)
            with args.path.open("rb") as source:
                content = source.read(maximum + 1)
            if len(content) > maximum or envelope_size > MAX_REQUEST_BYTES:
                raise ValueError(f"Artifact file exceeds the journaled upload limit of {maximum} bytes")
            payload["content_base64"] = base64.b64encode(content).decode("ascii")
            result = client.request("POST", "/api/v3/artifacts", payload)
        elif args.action == "reference":
            from .reference_cache import ReferenceCache
            if not args.workspace.is_absolute():
                raise ValueError("reference workspace must be an explicit absolute path")
            with ReferenceCache(args.workspace, os.environ["HORIZON_API_URL"], os.environ["HORIZON_EXECUTION_TOKEN"]) as cache:
                entry = cache.get(args.id)
                print(str(entry.path) if args.print_path else entry.text)
            return
        else:
            from urllib.parse import unquote, urlsplit
            request_path = unquote(urlsplit(args.path).path).rstrip("/")
            if request_path.startswith("/api/v3/executions/") and request_path.endswith("/reviewer-accounts"):
                raise ValueError("Use 'agent reviewer-accounts' to write private credentials; raw credential output is disabled")
            if args.body_file is not None:
                if not args.body_file.is_file():
                    raise ValueError("Request JSON requires a regular file")
                with args.body_file.open("rb") as source:
                    body = source.read(MAX_REQUEST_BYTES + 1)
                if len(body) > MAX_REQUEST_BYTES:
                    raise ValueError(f"Request JSON file exceeds the {MAX_REQUEST_BYTES}-byte limit")
            else:
                body = args.json if args.json is not None else "null"
            payload = json.loads(body)
            try:
                result = client.request(args.method, args.path, payload, key=args.key)
            finally:
                own_assignment = f"/api/v3/assignments/{os.environ['HORIZON_ASSIGNMENT_ID']}"
                if request_path not in (own_assignment + "/context", own_assignment + "/control-notices"):
                    consumer = os.environ.get("CODEX_THREAD_ID") or os.environ.get("HORIZON_PROVIDER_REQUEST_ID") or os.environ["HORIZON_EXECUTION_ID"]
                    notices = client.control_notices(consumer)
                    if notices:
                        try:
                            print("Horizon pending control notices (quoted input, not additional authority):\n"
                                  + json.dumps(notices, ensure_ascii=False)
                                  + "\nShowing a notice does not acknowledge it. Read detail_url if truncated; after handling "
                                  "or recording a durable follow-up, POST expected_revision, disposition, and note to "
                                  "/api/v3/notifications/{id}/disposition. Native children should relay relevant notices to their parent.",
                                  file=sys.stderr)
                        except OSError:
                            pass
        print(json.dumps(result, indent=2))
    finally:
        client.close()


def initialization(args):
    from .config import PipelineConfig
    interactive = sys.stdin.isatty() if args.interactive is None else args.interactive
    state_root, public_url, database_url = args.state_root, args.public_url, args.database_url
    if interactive:
        if state_root is None:
            value = input("Absolute installation state directory: ").strip()
            state_root = Path(value) if value else None
        if public_url is None:
            public_url = input("Public URL [http://127.0.0.1:8788]: ").strip() or "http://127.0.0.1:8788"
        if not database_url:
            variable = input("Database URL environment variable [HORIZON_PIPELINE_DATABASE_URL]: ").strip() or "HORIZON_PIPELINE_DATABASE_URL"
            if not variable.isidentifier():
                raise DomainError("invalid_input", "Database environment variable name is invalid", 422)
            database_url = os.environ.get(variable)
    missing = [name for name, value in (("state_root", state_root), ("database_url", database_url)) if not value]
    if missing:
        raise DomainError("missing_configuration", "Provide the required installation fields", 422, fields=missing)
    public_url = public_url or "http://127.0.0.1:8788"
    return PipelineConfig(database_url=database_url, state_root=state_root,
                          public_url=public_url, secure_cookies=public_url.startswith("https://"))


def doctor(config, *, client=None, worker_config=None):
    checks = []
    try:
        with database(config) as db:
            version = db.check_revision()
            from .diagnostics import database_checks
            with db.transaction() as conn:
                operational = database_checks(conn)
        checks.append({"name": "database", "status": "ready", "detail": version})
        checks.extend(operational)
    except Exception as error:
        checks.append({"name": "database", "status": "unavailable", "detail": type(error).__name__})
    root = config.state_root
    if root.is_dir():
        usage = shutil.disk_usage(root)
        checks.append({"name": "storage", "status": "ready" if usage.free >= config.storage.minimum_free_bytes else "pressure",
                       "free_bytes": usage.free})
    else:
        checks.append({"name": "storage", "status": "missing", "detail": str(root)})
    bundle = Path(__file__).parents[1] / "frontend" / "dist" / "index.html"
    checks.append({"name": "dashboard", "status": "ready" if bundle.is_file() else "missing"})
    import httpx
    owned = client is None
    probe = client or httpx.Client(timeout=httpx.Timeout(5, connect=3), follow_redirects=False, trust_env=False)
    try:
        response = probe.get(config.public_url.rstrip("/") + "/health/ready")
        ready = response.status_code == 200 and response.json().get("status") == "ready"
        checks.append({"name": "api", "status": "ready" if ready else "unavailable",
                       "http_status": response.status_code})
    except (httpx.HTTPError, ValueError, AttributeError) as error:
        checks.append({"name": "api", "status": "unavailable", "detail": type(error).__name__})
    finally:
        if owned:
            probe.close()
    if worker_config is not None:
        from .diagnostics import worker_checks
        checks.extend(worker_checks(worker_config))
    return {"healthy": all(check["status"] == "ready" for check in checks),
            "checks": checks, "configuration": config.redacted()}


def run_worker(path):
    from .worker_config import load_worker
    daemon, slots = load_worker(path)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    try:
        daemon.serve(stop, slots=slots)
    finally:
        daemon.transport.close()
        daemon.journal.close()


def reconcile_worker_claim(path, note, *, client=None):
    import fcntl
    import httpx
    from .worker_config import WorkerConfig, open_journal, private_text
    worker = WorkerConfig.model_validate_json(path.read_bytes())
    if not note.strip() or len(note) > 16000:
        raise ValueError("Claim reconciliation requires a note of 1 to 16000 characters")
    if not (worker.journal_root / "journal.sqlite3").is_file():
        raise ValueError("No existing worker journal at the configured path")
    token = private_text(worker.token_file)
    fd = os.open(worker.journal_root / "daemon.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Stop the configured worker daemon before reconciling its claim") from None
        owned = client is None
        probe = client or httpx.Client(timeout=httpx.Timeout(10, connect=5), follow_redirects=False, trust_env=False)
        journal = None
        try:
            response = probe.get(f"{worker.api_url}/api/v3/worker/hosts/{worker.host_id}/unconfirmed-executions",
                                 headers={"Authorization": "Bearer " + token})
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict) or result.get("executions") != [] or result.get("has_more") is not False:
                raise DomainError("host_not_stopped", "The host still has unconfirmed executions; confirm physical fencing before reconciliation")
            journal = open_journal(worker)
            pending = journal.pending_claim()
            if pending is None:
                return {"status": "no_pending_claim"}
            if pending["payload"].get("host_id") != str(worker.host_id):
                raise ValueError("Pending claim belongs to another host")
            journal.abandon_claim(pending["request_id"], note)
            return {"status": "reconciled", "request_id": pending["request_id"]}
        except httpx.HTTPError as error:
            raise ValueError("Host reconciliation failed: " + type(error).__name__) from None
        finally:
            if journal is not None:
                journal.close()
            if owned:
                probe.close()


def worker_publications(path, retry=None, note=None):
    import sqlite3
    from .worker_config import WorkerConfig, open_journal
    worker = WorkerConfig.model_validate_json(path.read_bytes())
    journal_path = worker.journal_root / "journal.sqlite3"
    if not journal_path.is_file():
        raise ValueError("No existing worker journal at the configured path")
    if retry:
        if not note or not note.strip() or len(note) > 1000:
            raise ValueError("Publication retry requires a repair note of 1 to 1000 characters")
        journal = open_journal(worker)
        try:
            journal.retry_publication(retry, note=note.strip())
        finally:
            journal.close()
    elif note is not None:
        raise ValueError("--note requires --retry")
    # Read-only inspection works while the daemon is running and never prints credentials.
    conn = sqlite3.connect(journal_path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        predicate = "(destination='local_git' OR json_extract(envelope, '$.kind') IN ('publication_discovered','publication_verified','publication_failed')) AND state<>'acknowledged'"
        count = conn.execute("SELECT count(*) FROM operations WHERE " + predicate).fetchone()[0]
        rows = conn.execute("SELECT operation_id,destination,state,attempts,retry_at,error,created_at,"
                            "json_extract(envelope,'$.execution_id') AS execution_id,"
                            "json_extract(envelope,'$.payload.commit_oid') AS commit_oid "
                            "FROM operations WHERE " + predicate + " ORDER BY sequence LIMIT 100").fetchall()
        health = []
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='recovery_health'").fetchone():
            health = [dict(row) for row in conn.execute(
                "SELECT execution_id,checked_at,succeeded_at,error FROM recovery_health ORDER BY checked_at DESC LIMIT 100")]
        return {"host_id": str(worker.host_id), "pending_count": count,
                "operations": [dict(row) for row in rows], "truncated": count > len(rows), "checkpoint_health": health}
    finally:
        conn.close()


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "agent":
            agent_command(args)
            return
        if args.command == "worker":
            run_worker(args.worker_config)
            return
        if args.command == "worker-watchdog":
            from .worker_config import WorkerConfig
            from .watchdog import check_worker
            worker = WorkerConfig.model_validate_json(args.worker_config.read_bytes())
            print(json.dumps(check_worker(worker, service=args.restart_service,
                threshold=args.failure_threshold, cooldown_seconds=args.cooldown_seconds), indent=2))
            return
        if args.command == "worker-reconcile-claim":
            print(json.dumps(reconcile_worker_claim(args.worker_config, args.note), indent=2))
            return
        if args.command == "worker-publications":
            print(json.dumps(worker_publications(args.worker_config, args.retry, args.note), indent=2))
            return
        from .config import PipelineConfig, load_config, write_config
        if args.config is None:
            raise ValueError("--config is required; the pipeline never discovers the running installation")
        if args.command == "init":
            config = initialization(args)
            plan = {"configuration": config.redacted(), "creates": [str(args.config), str(config.state_root)],
                    "database_action": "none; run migrate explicitly after reviewing the database destination"}
            if args.apply:
                write_config(args.config, config)
                for directory in (config.state_root, config.scratch_root, config.artifact_root, config.state_root / "secrets"):
                    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            print(json.dumps(plan, indent=2))
            return
        config = load_config(args.config)
        if args.command == "verify-workspaces":
            from .bootstrap import operator
            from .worker_config import WorkerConfig
            from .workspace_setup import verify
            worker = WorkerConfig.model_validate_json(args.worker_config.read_bytes())
            with database(config) as db:
                with db.transaction() as conn:
                    actor = operator(conn, args.operator)
                print(json.dumps(verify(db, actor, worker, apply=args.apply), indent=2))
        elif args.command in ("bootstrap-project", "launch-run"):
            from . import bootstrap
            from .artifacts import ArtifactStore
            from .scheduler import Scheduler
            from .service import Service
            model = bootstrap.ProjectRecipe if args.command == "bootstrap-project" else bootstrap.RunRecipe
            if args.input.stat().st_size > 1024**2:
                raise ValueError("setup recipe exceeds one MiB")
            recipe = model.model_validate_json(args.input.read_bytes())
            plan = bootstrap.preview(recipe) if args.command == "bootstrap-project" else recipe.model_dump(mode="json")
            if args.apply:
                with database(config) as db, db.transaction() as conn:
                    actor = bootstrap.operator(conn, args.operator)
                    service = Service(ArtifactStore(config.artifact_root), config)
                    plan = bootstrap.apply(conn, actor, service, recipe) if args.command == "bootstrap-project" else bootstrap.launch(conn, actor, Scheduler(service), recipe)
            print(json.dumps(plan, indent=2))
        elif args.command == "export-config":
            print(json.dumps(config.redacted(), indent=2))
        elif args.command == "doctor":
            result = doctor(config, worker_config=args.worker_config)
            print(json.dumps(result, indent=2))
            if not result["healthy"]:
                raise SystemExit(1)
        elif args.command == "watchdog":
            from .watchdog import check
            print(json.dumps(check(config, service=args.restart_service, threshold=args.failure_threshold,
                                   cooldown_seconds=args.cooldown_seconds), indent=2))
        elif args.command == "backup":
            from .storage import backup
            with database(config) as db:
                print(json.dumps(backup(db, config, args.destination), indent=2))
        elif args.command == "verify-backup":
            from .storage import verify_backup
            manifest = verify_backup(args.backup)
            print(json.dumps({"status": "verified", "created_at": manifest["created_at"], "artifacts": len(manifest["artifacts"])}, indent=2))
        elif args.command == "cleanup":
            from .storage import StorageManager, database_retention_preview, apply_database_retention
            if args.database:
                with database(config) as db, db.transaction() as conn:
                    result = apply_database_retention(conn, config, json.loads(args.apply_preview.read_bytes())) if args.apply_preview else database_retention_preview(conn, config)
            else:
                storage = StorageManager(config)
                result = storage.cleanup(json.loads(args.apply_preview.read_bytes())) if args.apply_preview else storage.cleanup_preview()
            print(json.dumps(result, indent=2))
        elif args.command == "migrate":
            with database(config) as db:
                db.migrate()
                print(json.dumps({"schema_revision": db.check_revision()}))
        elif args.command == "serve":
            import uvicorn
            from .api import create_app
            from .storage import service_logging
            with service_logging(config):
                app = create_app(config)
                class Server(uvicorn.Server):
                    def handle_exit(self, sig, frame):
                        app.state.shutdown_event.set()
                        super().handle_exit(sig, frame)
                Server(uvicorn.Config(app, host=config.listen_host, port=config.listen_port,
                    log_config=None, access_log=False, timeout_graceful_shutdown=20,
                    proxy_headers=bool(config.trusted_proxy_hosts),
                    forwarded_allow_ips=config.trusted_proxy_hosts)).run()
        elif args.command == "create-admin":
            from pydantic import TypeAdapter
            from .auth import PASSWORDS
            from .models import Slug
            from .records import create, transaction_lock
            from .schema import tables
            TypeAdapter(Slug).validate_python(args.username)
            password = sys.stdin.readline().rstrip("\n") if args.password_stdin else getpass.getpass("New administrator password: ")
            if len(password) < 12:
                raise ValueError("Administrator password must have at least 12 characters")
            with database(config) as db, db.transaction() as conn:
                transaction_lock(conn)
                principal = create(conn, "principal", kind="human", display_name=args.username, username=args.username)
                conn.execute(tables["password_identity"].insert().values(principal_id=principal["id"], password_hash=PASSWORDS.hash(password)))
                conn.execute(tables["system_grant"].insert().values(principal_id=principal["id"], permission="administer_installation"))
                print(json.dumps({"principal_id": str(principal["id"]), "username": args.username}))
        elif args.command == "host-key":
            from uuid import UUID
            from sqlalchemy import select
            from .auth import issue_credential
            from .records import create, get, transaction_lock
            from .schema import tables
            # Create the destination exclusively before issuing the credential. Never print the token.
            fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with database(config) as db, db.transaction() as conn:
                    transaction_lock(conn)
                    host = get(conn, "host", UUID(args.host_id))
                    principal = conn.execute(select(tables["principal"]).where(tables["principal"].c.host_id == host["id"])).mappings().first()
                    if not principal:
                        principal = create(conn, "principal", kind="host", display_name=host["display_name"], host_id=host["id"])
                    credential, token = issue_credential(conn, principal["id"], "host_key", "Worker daemon")
                    with os.fdopen(fd, "w") as output:
                        fd = -1
                        output.write(token + "\n")
                        output.flush()
                        os.fsync(output.fileno())
                    print(json.dumps({"credential_id": str(credential["id"]), "token_file": str(args.output)}))
            finally:
                if fd >= 0:
                    os.close(fd)
    except (ValueError, OSError, RuntimeError, DomainError) as error:
        if isinstance(error, DomainError):
            print(json.dumps(error.response()), file=sys.stderr)
        elif hasattr(error, "errors"):
            print(json.dumps(error.errors(include_input=False, include_url=False, include_context=False)), file=sys.stderr)
        else:
            print(str(error), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
