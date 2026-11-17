"""Alembic environment. Migrations touch only the application's own "assistant_" tables.

The URL comes from DATABASE_URL (or the caller), never from alembic.ini. The
version table is "assistant_schema_version" so a customer's own Alembic
history in the same database is never read or changed.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool

from app.repository.postgres import VERSION_TABLE

config = context.config


def _url() -> str:
    url = config.attributes.get("database_url")
    if url:
        return url
    from app.config import get_settings

    settings = get_settings()
    if settings.database_url is None:
        raise SystemExit("DATABASE_URL is not set.")
    return settings.database_url.get_secret_value()


def run_migrations_offline() -> None:
    context.configure(url=_url(), literal_binds=True, version_table=VERSION_TABLE)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, version_table=VERSION_TABLE, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
