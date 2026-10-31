"""
Alembic environment configuration.

Supports both offline (SQL script generation) and online (live DB) migration modes.
Auto-generates migrations from SQLAlchemy models using ``target_metadata``.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# ---------------------------------------------------------------------------
# Alembic Config object
# ---------------------------------------------------------------------------
config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Override the sqlalchemy.url with the app's DATABASE_URL env var if set.
database_url = os.environ.get("DATABASE_URL")
if database_url:
    # Render.com PostgreSQL URLs use 'postgres://' but SQLAlchemy 1.4+ needs 'postgresql://'
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    config.set_main_option("sqlalchemy.url", database_url)

# ---------------------------------------------------------------------------
# Import all models so Alembic can detect them for autogenerate.
# ---------------------------------------------------------------------------
from app.database import Base  # noqa: E402
import app.models.job  # noqa: F401, E402
import app.models.link  # noqa: F401, E402
import app.models.log  # noqa: F401, E402
import app.zoho.auth  # noqa: F401, E402  (registers ZohoToken)

target_metadata = Base.metadata


# ---------------------------------------------------------------------------
# Offline migration
# ---------------------------------------------------------------------------


def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode.

    This configures the context with just a URL and not an Engine, though an
    Engine is acceptable here as well.  By skipping the Engine creation we
    don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the script output.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


# ---------------------------------------------------------------------------
# Online migration
# ---------------------------------------------------------------------------


def run_migrations_online() -> None:
    """
    Run migrations in 'online' mode.

    In this scenario we need to create an Engine and associate a connection
    with the context.
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

