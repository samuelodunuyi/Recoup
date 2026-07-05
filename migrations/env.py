"""Alembic environment (#10).

Pulls the database URL from the app settings and runs migrations with psycopg 3.
Production schema management path:  `alembic upgrade head`.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine

from app.config import get_settings


def _sqlalchemy_url() -> str:
    # SQLAlchemy needs the psycopg-3 driver spelled out explicitly.
    url = get_settings().database_url
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def run_migrations_offline() -> None:
    context.configure(url=_sqlalchemy_url(), literal_binds=True,
                      dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_sqlalchemy_url())
    with engine.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
