from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SQLITE_DEV_PATH = _REPO_ROOT / "web" / "data" / "diffdx.db"


def resolve_database_url() -> str:
    """Read DATABASE_URL from the environment; fall back to a local SQLite file.

    Never hardcode a URL here — env.py and the app both call this so there is
    one source of truth for how the DB is chosen.
    """
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        _SQLITE_DEV_PATH.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{_SQLITE_DEV_PATH}"

    # Render/Railway hand out "postgres://" or "postgresql://"; SQLAlchemy
    # needs the driver named explicitly to use psycopg 3.
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://") and "+psycopg" not in url:
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        url = resolve_database_url()
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        _engine = create_engine(url, pool_pre_ping=True, connect_args=connect_args)
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _SessionLocal


def get_session() -> Session:
    """FastAPI dependency: yields a Session, closes it after the request."""
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.close()
