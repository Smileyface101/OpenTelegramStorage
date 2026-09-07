"""Alembic environment. Migrations run over a plain (sync) sqlite URL derived
from OTS_DATA_DIR so the same data directory the app uses gets migrated."""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app import config as app_config
from app.db import Base
from app import models  # noqa: F401  (register tables)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

app_config.ensure_dirs()
config.set_main_option("sqlalchemy.url", f"sqlite:///{app_config.DB_PATH}")
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=config.get_main_option("sqlalchemy.url"), target_metadata=target_metadata,
                      literal_binds=True, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(config.get_section(config.config_ini_section, {}),
                                     prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
