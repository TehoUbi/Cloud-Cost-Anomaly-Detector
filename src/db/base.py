"""Declarative base, kept separate from the engine.

Alembic and the test suite need ``Base.metadata`` without paying for a live
engine or a ``DATABASE_URL``, so the two concerns do not share a module.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def new_id() -> str:
    """Primary keys are UUID4 strings — readable in logs, portable across
    Postgres and the SQLite used by the test suite."""
    return str(uuid.uuid4())
