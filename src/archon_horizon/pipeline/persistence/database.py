"""Explicit PostgreSQL lifecycle, with bounded pools and no startup migration."""

from __future__ import annotations

import re
import logging
import time
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import make_url

from .._resources import MIGRATIONS_ROOT
from .schema import metadata, tables


class Database:
    """Own a bounded PostgreSQL pool and explicit transaction/migration boundaries.

    Constructing a pool does not install a schema. Operators migrate deliberately;
    services check the installed revision before accepting work.
    """

    def __init__(
        self,
        url: str,
        *,
        schema: str = "public",
        pool_size: int = 5,
        max_overflow: int = 0,
        pool_timeout: float = 10,
        statement_timeout_ms: int = 30_000,
    ) -> None:
        parsed = make_url(url)
        if parsed.drivername not in {"postgresql", "postgresql+psycopg"}:
            raise ValueError("the pipeline requires PostgreSQL with psycopg")
        if not parsed.database:
            raise ValueError("an explicit PostgreSQL database name is required")
        # Schema names are interpolated into connection options and DDL, where
        # value bind parameters cannot stand in for identifiers. Restrict them
        # to a lowercase ASCII subset of PostgreSQL's 63-byte identifier limit.
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema):
            raise ValueError("schema must be a lowercase PostgreSQL identifier")
        if pool_size < 1 or max_overflow < 0 or pool_timeout <= 0 or statement_timeout_ms < 1:
            raise ValueError("database pool and timeout settings must be positive and bounded")
        self.schema = schema
        # A fixed pool (zero overflow by default) bounds database concurrency;
        # acquisition timeouts turn saturation into a visible failure. Pre-ping
        # detects dead connections; recycling after 1800 s (30 min) limits age.
        self.engine: Engine = create_engine(
            parsed.set(drivername="postgresql+psycopg"),
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_timeout=pool_timeout,
            pool_pre_ping=True,
            pool_recycle=1800,
            connect_args={
                "connect_timeout": 10,
                "options": (
                    f"-c search_path={schema} -c timezone=UTC "
                    f"-c statement_timeout={statement_timeout_ms} "
                    # Cancel sessions idle inside a transaction for one minute
                    # so abandoned transactions cannot hold locks indefinitely.
                    "-c idle_in_transaction_session_timeout=60000"
                ),
            },
            # Keep bound values, which may contain credentials or private data,
            # out of SQLAlchemy's statement/error representations.
            hide_parameters=True,
        )

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        """Commit on success or roll back on error, then release the connection."""
        with self.engine.begin() as connection:
            connection.info.pop("horizon_mutation_lock_started", None)
            try:
                yield connection
            finally:
                started = connection.info.pop("horizon_mutation_lock_started", None)
                # One second is an observability threshold, not a transaction
                # deadline: warn about long mutation-lock holds without aborting.
                if started is not None and time.monotonic() - started >= 1:
                    logging.getLogger(__name__).warning("Mutation lock held %.3fs", time.monotonic() - started)

    def migrate(self) -> None:
        """Install/upgrade only this explicitly selected database and schema.

        This must be called by an operator migration command, never by API or
        worker startup. A transaction advisory lock serializes concurrent runs.
        """
        from alembic import command
        from alembic.config import Config

        config = Config()
        config.set_main_option("script_location", str(MIGRATIONS_ROOT))
        config.attributes["schema"] = self.schema
        with self.engine.begin() as connection:
            connection.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                               {"key": f"archon-horizon-pipeline-migration:{self.schema}"})
            connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{self.schema}"'))
            config.attributes["connection"] = connection
            command.upgrade(config, "head")

    def check_revision(self) -> str:
        """Fail startup visibly if the operator has not applied migrations."""
        from alembic.config import Config
        from alembic.migration import MigrationContext
        from alembic.script import ScriptDirectory

        config = Config()
        config.set_main_option("script_location", str(MIGRATIONS_ROOT))
        head = ScriptDirectory.from_config(config).get_current_head()
        with self.engine.connect() as connection:
            current = MigrationContext.configure(connection, opts={"version_table_schema": self.schema}).get_current_revision()
        if current != head:
            raise RuntimeError(f"pipeline database migration required: installed={current}, expected={head}")
        assert current is not None
        return current

    def close(self) -> None:
        self.engine.dispose()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


__all__ = ["Database", "metadata", "tables"]
