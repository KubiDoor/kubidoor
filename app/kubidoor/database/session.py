"""Engine/session management and migrations."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from kubidoor.core.exceptions import ConfigurationError

SessionFactory = sessionmaker[Session]


def create_db_engine(database_url: str) -> Engine:
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite":
        kwargs: dict[str, Any] = {"connect_args": {"check_same_thread": False, "timeout": 30}}
        if url.database in (None, "", ":memory:"):
            kwargs["poolclass"] = StaticPool
        engine = create_engine(url, **kwargs)

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            if url.database not in (None, "", ":memory:"):
                cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

        return engine
    return create_engine(url, pool_pre_ping=True)


def create_session_factory(engine: Engine) -> SessionFactory:
    # expire_on_commit=False lets callers read returned objects after the transaction ends.
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def session_scope(factory: SessionFactory) -> Iterator[Session]:
    """One transaction: commit on success, rollback on any error."""
    session = factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def check_database(engine: Engine) -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return False
    return True


def render_url(database_url: str) -> str:
    """URL with the password masked, safe for logs."""
    return make_url(database_url).render_as_string(hide_password=True)


def run_migrations(database_url: str, migrations_dir: str) -> None:
    script_dir = Path(migrations_dir).resolve()
    if not (script_dir / "env.py").is_file():
        raise ConfigurationError(f"Alembic migrations directory not found: {script_dir}")
    cfg = Config()
    cfg.set_main_option("script_location", str(script_dir))
    cfg.attributes["database_url"] = database_url
    command.upgrade(cfg, "head")
