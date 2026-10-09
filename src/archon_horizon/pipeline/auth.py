"""Opaque credentials and scoped authorization using the authoritative execution lease."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import secrets
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import and_, func, or_, select, update

from .errors import DomainError
from .persistence.schema import tables

# Human passwords use Argon2's deliberately expensive hashing. Library defaults
# define the current policy; login upgrades stored hashes when that policy changes.
PASSWORDS = PasswordHasher()
# Project roles are cumulative. Installation administration is a separate grant,
# never an extra level implicitly available to maintainers or execution tokens.
ROLE_ORDER = {"viewer": 0, "worker": 1, "maintainer": 2}


@dataclass(frozen=True)
class Actor:
    """Authenticated identity; project access and execution liveness need rechecks."""

    id: UUID
    kind: str
    owner: dict
    credential_kind: str
    credential_id: UUID | None = None


def token_digest(token: str) -> str:
    """Hash a high-entropy bearer token for lookup without storing its raw value.

    SHA-256 is suitable for random tokens; human passwords require the slower
    Argon2 path above because they can have far less entropy.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def issue_credential(conn, principal_id: UUID, kind: str, name: str,
                     expires_at: datetime | None = None) -> tuple[dict, str]:
    """Persist the token digest and return the secret to its caller for delivery."""
    # 32 random bytes provide 256 bits of entropy before URL-safe encoding.
    # The hz_ prefix identifies the token family; it contributes no secrecy.
    token = "hz_" + secrets.token_urlsafe(32)
    table = tables["credential"]
    row = conn.execute(table.insert().values(
        principal_id=principal_id, kind=kind, name=name,
        token_hash=token_digest(token), display_prefix=token[:10], expires_at=expires_at,
    ).returning(table)).mappings().one()
    return dict(row), token


def authenticate(conn, token: str | None) -> Actor:
    """Resolve a nonrevoked credential and enabled principal using database time."""
    # Reject oversized input before hashing or querying. 512 is an input bound,
    # deliberately larger than the tokens we issue, not a token format version.
    if not token or len(token) > 512:
        raise DomainError("unauthenticated", "Sign in to Horizon", 401)
    credential, principal = tables["credential"], tables["principal"]
    row = conn.execute(select(
        credential.c.id.label("credential_id"), credential.c.kind.label("credential_kind"),
        principal.c.id, principal.c.kind, principal.c.username, principal.c.host_id,
        principal.c.execution_id, principal.c.service_name,
    ).join(principal, credential.c.principal_id == principal.c.id).where(
        credential.c.token_hash == token_digest(token), credential.c.revoked_at.is_(None),
        or_(credential.c.expires_at.is_(None), credential.c.expires_at > func.now()),
        principal.c.disabled_at.is_(None),
    )).mappings().first()
    if not row:
        raise DomainError("unauthenticated", "Credential is expired or unavailable", 401)
    values = dict(row)
    owner = {key: str(values.pop(key)) for key in ("username", "host_id", "execution_id", "service_name")
             if values.get(key) is not None}
    for key in ("username", "host_id", "execution_id", "service_name"):
        values.pop(key, None)
    return Actor(owner=owner, **values)


def is_admin(conn, actor: Actor) -> bool:
    if actor.kind not in ("human", "service"):
        return False
    grant = tables["system_grant"]
    return bool(conn.execute(select(grant.c.principal_id).where(
        grant.c.principal_id == actor.id,
        grant.c.permission == "administer_installation",
    )).first())


def require_admin(conn, actor: Actor) -> None:
    if not is_admin(conn, actor):
        raise DomainError("forbidden", "Installation administrator permission is required", 403)


def live_execution(conn, actor: Actor, *, lock: bool = False) -> dict:
    """Recheck lease-backed agent authority, optionally locking its execution row.

    A valid token alone is insufficient: the execution and assignment must still
    be live. Database time keeps this decision independent of worker clock skew.
    Callers making a fenced mutation can request the row lock in their transaction.
    """
    if actor.kind != "agent" or actor.credential_kind != "execution_token":
        raise DomainError("forbidden", "An active execution credential is required", 403)
    execution, assignment, run, mission = (tables[name] for name in (
        "execution", "assignment", "run", "mission"))
    query = select(execution, assignment.c.run_id, assignment.c.role,
                   assignment.c.mission_id, mission.c.project_id).join(
        assignment, execution.c.assignment_id == assignment.c.id
    ).join(run, assignment.c.run_id == run.c.id).join(mission, run.c.mission_id == mission.c.id).where(
        execution.c.id == UUID(actor.owner["execution_id"]),
        execution.c.status.in_(("starting", "running")),
        execution.c.lease_expires_at > func.now(), assignment.c.status == "running",
        run.c.status.in_(("active", "paused", "draining")),
    )
    if lock:
        query = query.with_for_update(of=execution)
    row = conn.execute(query).mappings().first()
    if not row:
        raise DomainError("stale_epoch", "Execution authority has expired or been cancelled", 409)
    return dict(row)


def require_project(conn, actor: Actor, project_id: UUID, role: str = "viewer") -> None:
    if is_admin(conn, actor):
        return
    if actor.kind == "agent":
        execution = live_execution(conn, actor)
        if execution["project_id"] == project_id and ROLE_ORDER[execution["role"]] >= ROLE_ORDER[role]:
            return
    elif actor.kind in ("human", "service"):
        grant = tables["project_grant"]
        actual = conn.execute(select(grant.c.role).where(
            grant.c.principal_id == actor.id, grant.c.project_id == project_id,
        )).scalar_one_or_none()
        if actual is not None and ROLE_ORDER[actual] >= ROLE_ORDER[role]:
            return
    raise DomainError("forbidden", "The credential does not grant this project operation", 403)


def require_host(conn, actor: Actor, host_id: UUID) -> None:
    """Require this exact host's key; project membership grants no host authority."""
    if (actor.kind != "host" or actor.credential_kind != "host_key"
            or UUID(actor.owner["host_id"]) != host_id):
        raise DomainError("forbidden", "This host credential cannot operate another host", 403)


def login(conn, username: str, password: str, *, session_seconds: int) -> tuple[dict, str]:
    """Verify a human password and issue a separate, expiring browser credential."""
    principal, identity = tables["principal"], tables["password_identity"]
    row = conn.execute(select(principal, identity.c.password_hash).join(
        identity, principal.c.id == identity.c.principal_id
    ).where(principal.c.kind == "human", principal.c.username == username,
            principal.c.disabled_at.is_(None))).mappings().first()
    # Verify a fixed valid hash even for unknown accounts to avoid the fast lookup path.
    candidate = row["password_hash"] if row else _DUMMY_HASH
    try:
        valid = PASSWORDS.verify(candidate, password)
    except (VerificationError, InvalidHashError):
        valid = False
    if not valid or not row:
        raise DomainError("invalid_credentials", "Username or password is incorrect", 401)
    if PASSWORDS.check_needs_rehash(candidate):
        conn.execute(update(identity).where(identity.c.principal_id == row["id"]).values(
            password_hash=PASSWORDS.hash(password), updated_at=func.now()))
    credential, token = issue_credential(conn, row["id"], "browser_session", "Dashboard",
        datetime.now(timezone.utc) + timedelta(seconds=session_seconds))
    return dict(row), token


# Public, valid Argon2id fixture for the unknown-user verification path, not an
# account credential. Its encoded memory/time/parallelism costs make failed
# lookups perform password-hashing work too; it does not grant access to anyone.
_DUMMY_HASH = "$argon2id$v=19$m=65536,t=3,p=4$VVVWV1hZWltcXV5fYGFiYw$D5NIio36umltNkBhN8pXou46TUdgpdNcf81sUBclMQE"
