"""Alembic environment. The URL comes from (in order): programmatic config attribute,
KUBIDOOR_DATABASE_URL, then the application settings default."""

import os

from alembic import context

from kubidoor.database import models  # noqa: F401  (register tables on metadata)
from kubidoor.database.base import Base
from kubidoor.database.session import create_db_engine

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    url = config.attributes.get("database_url") or os.environ.get("KUBIDOOR_DATABASE_URL")
    if url:
        return str(url)
    from kubidoor.core.config import Settings

    return Settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_db_engine(_url())
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
