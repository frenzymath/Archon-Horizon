"""Operator configuration. Loading configuration has no installation side effects."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from sqlalchemy.engine import make_url


class StoragePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    diagnostic_retention_seconds: int = Field(default=7 * 86400, ge=1)
    failure_diagnostic_retention_seconds: int = Field(default=30 * 86400, ge=1)
    provider_resume_retention_seconds: int = Field(default=30 * 86400, ge=1)
    event_replay_retention_seconds: int = Field(default=7 * 86400, ge=1)
    max_offline_replay_seconds: int = Field(default=7 * 86400, ge=1)
    idempotency_retention_seconds: int = Field(default=14 * 86400, ge=1)
    cache_budget_bytes: int = Field(default=4 * 1024**3, ge=0)
    diagnostic_budget_bytes: int = Field(default=1024**3, ge=0)
    service_log_budget_bytes: int = Field(default=512 * 1024**2, ge=0)
    journal_bytes: int = Field(default=512 * 1024**2, ge=1024**2)
    minimum_free_bytes: int = Field(default=2 * 1024**3, ge=0)
    # This is an operator cleanup target, not an admission floor.  The byte
    # floor and pause_used_percent remain the emergency execution guards.
    cleanup_target_free_percent: int = Field(
        default=20, ge=0, le=90,
        validation_alias=AliasChoices("cleanup_target_free_percent", "minimum_free_percent"))
    warn_used_percent: int = Field(default=75, ge=1, le=99)
    pause_used_percent: int = Field(default=85, ge=1, le=99)
    backup_keep_count: int = Field(default=7, ge=1)
    backup_budget_bytes: int = Field(default=16 * 1024**3, ge=0)
    image_keep_previous_count: int = Field(default=1, ge=0)

    @model_validator(mode="after")
    def coherent(self):
        if self.idempotency_retention_seconds <= self.max_offline_replay_seconds:
            raise ValueError("idempotency retention must exceed the offline retry window")
        if self.warn_used_percent >= self.pause_used_percent:
            raise ValueError("warn_used_percent must be below pause_used_percent")
        if 0 < self.service_log_budget_bytes < 4096:
            raise ValueError("service_log_budget_bytes must be zero or at least 4096")
        return self


class IntegrationBrowserAuth(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["forgejo_proxy", "zulip_jwt"]
    identities: dict[UUID, str] = Field(default_factory=dict)
    credential_ref: str | None = None
    session_cookie_name: str | None = None

    @model_validator(mode="after")
    def explicit_identity(self):
        pattern = r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,99}" if self.mode == "forgejo_proxy" else r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+"
        if any(len(value) > 254 or not re.fullmatch(pattern, value) for value in self.identities.values()):
            raise ValueError("browser integration identities must be exact usernames or email addresses")
        if self.mode == "zulip_jwt":
            if not self.credential_ref or not re.fullmatch(r"secret:[A-Za-z0-9_-]{1,100}", self.credential_ref):
                raise ValueError("Zulip browser login requires an explicit secret credential_ref")
            if self.session_cookie_name is not None:
                raise ValueError("Zulip uses its standard session cookie names")
        elif self.credential_ref is not None:
            raise ValueError("Forge proxy login does not use a credential_ref")
        if self.session_cookie_name is not None and (not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", self.session_cookie_name)
                or self.session_cookie_name in {"horizon_session", "sessionid", "csrftoken", "__Host-sessionid", "__Host-csrftoken", "_csrf"}):
            raise ValueError("Forge needs a distinct session cookie name")
        return self


class PipelineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    database_url: SecretStr
    state_root: Path
    listen_host: str = "127.0.0.1"
    listen_port: int = Field(default=8788, ge=1, le=65535)
    public_url: str = "http://127.0.0.1:8788"
    integration_public_urls: dict[UUID, str] = Field(default_factory=dict)
    reviewer_account_admin_credentials: dict[UUID, str] = Field(default_factory=dict)
    integration_browser_auth: dict[UUID, IntegrationBrowserAuth] = Field(default_factory=dict)
    integration_browser_binding_credential_ref: str | None = None
    trusted_proxy_hosts: list[str] = Field(default_factory=list)
    secure_cookies: bool = True
    database_pool_size: int = Field(default=8, ge=1, le=64)
    database_pool_timeout_seconds: int = Field(default=5, ge=1, le=30)
    statement_timeout_seconds: int = Field(default=10, ge=1, le=60)
    lease_seconds: int = Field(default=90, ge=15, le=600)
    host_stale_seconds: int = Field(default=120, ge=15, le=3600)
    scheduler_interval_seconds: float = Field(default=2, ge=0.1, le=60)
    # Model planning has its own cadence; the cheap scheduler poll is not a
    # request to spend another agent session on an unchanged frontier.
    planner_min_interval_seconds: int = Field(default=120, ge=1, le=86400)
    planner_max_idle_seconds: int = Field(default=1800, ge=1, le=86400)
    planner_max_unchanged_passes: int = Field(default=3, ge=1, le=100)
    automation_idle_recheck_seconds: int = Field(default=300, ge=0, le=86400)
    coordination_stall_seconds: int = Field(default=1800, ge=60, le=86400)
    coordination_repeat_passes: int = Field(default=3, ge=2, le=100)
    connector_interval_seconds: int = Field(default=30, ge=5, le=3600)
    session_seconds: int = Field(default=12 * 3600, ge=60, le=7 * 86400)
    max_request_bytes: int = Field(default=1024**2, ge=1024, le=16 * 1024**2)
    # Source documents stream to disk; this is independent of the small JSON
    # request/journal limit and cannot exceed the artifact store's blob bound.
    max_reference_file_bytes: int = Field(default=64 * 1024**2, ge=1, le=64 * 1024**2)
    storage: StoragePolicy = Field(default_factory=StoragePolicy)
    search_enabled: bool = False
    search_allowed_origins: list[str] = Field(default_factory=list)
    search_local_roots: list[Path] = Field(default_factory=list)
    search_workers: int = Field(default=2, ge=1, le=8)
    search_memory_budget_bytes: int = Field(default=512 * 1024**2, ge=1024**2)
    skill_source_root: Path | None = None

    @field_validator("integration_public_urls")
    @classmethod
    def public_integration_links(cls, values):
        from urllib.parse import urlsplit

        for value in values.values():
            parsed = urlsplit(value)
            if (not parsed.hostname or parsed.username or parsed.password or parsed.query
                    or parsed.fragment or parsed.scheme not in {"https", "http"}
                    or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})):
                raise ValueError("integration public URLs require HTTPS without credentials, queries or fragments")
        return values

    @field_validator("state_root")
    @classmethod
    def explicit_absolute_root(cls, path: Path) -> Path:
        if not path.is_absolute() or ".." in path.parts or path == Path("/"):
            raise ValueError("state_root must be an explicit absolute directory")
        return path

    @field_validator("skill_source_root")
    @classmethod
    def explicit_skill_root(cls, path: Path | None) -> Path | None:
        return cls.explicit_absolute_root(path) if path is not None else None

    @field_validator("database_url")
    @classmethod
    def postgres_only(cls, value: SecretStr) -> SecretStr:
        try:
            url = make_url(value.get_secret_value())
        except Exception:
            raise ValueError("database_url is not a valid PostgreSQL URL") from None
        if url.drivername != "postgresql+psycopg" or not url.database:
            raise ValueError("database_url requires postgresql+psycopg and an explicit database")
        return value

    @model_validator(mode="after")
    def local_http_only(self):
        from urllib.parse import urlsplit

        url = urlsplit(self.public_url)
        if url.username or url.password or not url.hostname or url.query or url.fragment:
            raise ValueError("public_url must be an origin without credentials")
        if url.path not in ("", "/"):
            raise ValueError("public_url must not contain a path")
        local = url.hostname in ("localhost", "127.0.0.1", "::1")
        if url.scheme != "https" and not (local and url.scheme == "http"):
            raise ValueError("public_url requires HTTPS except on loopback")
        if not self.secure_cookies and not local:
            raise ValueError("insecure cookies are allowed only for a loopback development URL")
        for identifier in self.integration_browser_auth:
            native = urlsplit(self.integration_public_urls.get(identifier, ""))
            if (not self.secure_cookies or url.scheme != "https" or native.scheme != "https"
                    or native.hostname != url.hostname or native.path not in ("", "/")):
                raise ValueError("browser integrations require explicit HTTPS origins on the Horizon hostname and secure cookies")
        if self.integration_browser_auth and (not self.integration_browser_binding_credential_ref or
                not re.fullmatch(r"secret:[A-Za-z0-9_-]{1,100}", self.integration_browser_binding_credential_ref)):
            raise ValueError("browser integrations require an explicit server-only binding credential_ref")
        return self

    @property
    def artifact_root(self) -> Path:
        return self.state_root / "artifacts"

    @property
    def scratch_root(self) -> Path:
        return self.state_root / "tmp"

    def redacted(self) -> dict:
        data = self.model_dump(mode="json")
        url = make_url(self.database_url.get_secret_value())
        data["database_url"] = url.set(query={key: value if key == "sslmode" else "[redacted]"
            for key, value in url.query.items()}).render_as_string(hide_password=True)
        return data


def load_config(path: Path) -> PipelineConfig:
    # A path is mandatory: never discover the live installation from HOME or cwd.
    return PipelineConfig.model_validate_json(path.read_bytes())


def write_config(path: Path, config: PipelineConfig) -> None:
    """Create once; setup never overwrites an operator's existing configuration."""
    data = config.model_dump(mode="json")
    data["database_url"] = config.database_url.get_secret_value()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
