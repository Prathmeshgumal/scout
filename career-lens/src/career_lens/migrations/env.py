"""Alembic's entry point.

`include_schemas=True` because v1 has two: `public` for the site-agnostic
capture tables and `linkedin` for what a profile means. `include_name` then has
to exclude everything we do not own -- Procrastinate creates and migrates its
own tables in its own schema, and autogenerate would otherwise offer to drop
every one of them.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from career_lens.store.schema import metadata

config = context.config
target_metadata = metadata

OURS = {None, "public", "linkedin", "google"}


def include_name(name, type_, parent_names):
    if type_ == "schema":
        return name in OURS
    return True


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata,
                          include_schemas=True, include_name=include_name)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
