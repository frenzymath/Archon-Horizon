from __future__ import annotations

from alembic import context


def run_migrations() -> None:
    config = context.config
    connection = config.attributes.get("connection")
    if connection is None:
        raise RuntimeError("migrations require an explicit Database.migrate() connection")

    context.configure(
        connection=connection,
        version_table_schema=config.attributes["schema"],
        transaction_per_migration=False,
    )
    with context.begin_transaction():
        context.run_migrations()


if hasattr(context, "config"):
    run_migrations()
