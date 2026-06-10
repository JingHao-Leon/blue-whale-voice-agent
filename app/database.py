"""
SQLite persistence via SQLModel. Two tables:

  Visitor        - one row per (plate, call), the historical record
  Conversation   - full transcript + tool calls for debugging / guard audit

We use aiosqlite so the FastAPI event loop never blocks on the DB.
"""
from __future__ import annotations

from datetime import datetime
from typing import AsyncIterator, Optional

from sqlalchemy import Column, DateTime, String, Text, func
from sqlmodel import Field, SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import get_settings
from app.logging import logger
from app.schemas import VisitorInfo


class Visitor(SQLModel, table=True):
    """One row per visit by a plate."""

    __tablename__ = "visitors"

    id: Optional[int] = Field(default=None, primary_key=True)
    plate: str = Field(index=True, sa_column_kwargs={"unique": False})
    company: Optional[str] = None
    reason: Optional[str] = None
    contact_name: Optional[str] = None
    phone: Optional[str] = None
    duration: Optional[str] = None
    is_returning: bool = False
    call_sid: str = Field(index=True)
    started_at: datetime = Field(default_factory=datetime.utcnow)
    ended_at: Optional[datetime] = None
    duration_seconds: Optional[float] = None


class Conversation(SQLModel, table=True):
    """Full transcript for a single call - useful for debugging and guard audit."""

    __tablename__ = "conversations"

    id: Optional[int] = Field(default=None, primary_key=True)
    call_sid: str = Field(index=True, unique=True)
    plate: Optional[str] = Field(default=None, index=True)
    transcript: str = Field(default="", sa_column=Column(Text))
    final_card: Optional[str] = Field(default=None, sa_column=Column(Text))
    started_at: datetime = Field(
        sa_column=Column(DateTime, server_default=func.now())
    )


async def init_db() -> None:
    """Create tables if they don't exist. Idempotent."""
    settings = get_settings()
    # ensure the parent dir exists
    if settings.database_url.startswith("sqlite"):
        from pathlib import Path
        db_path = settings.database_url.split("///", 1)[-1]
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    engine = _engine()
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    logger.info("Database ready at {}", settings.database_url)


def _engine():
    from sqlalchemy.ext.asyncio import create_async_engine
    return create_async_engine(get_settings().database_url, echo=False)


_engine_singleton: Optional[object] = None


def engine():
    global _engine_singleton
    if _engine_singleton is None:
        _engine_singleton = _engine()
    return _engine_singleton


async def get_session() -> AsyncIterator[AsyncSession]:
    async with AsyncSession(engine()) as session:
        yield session


async def find_returning_visitor(plate: str, limit: int = 3) -> list[VisitorInfo]:
    """Look up the most recent visits for a plate.

    Used at the start of the call to detect returning visitors and skip
    re-asking the same questions.
    """
    if not plate:
        return []
    async with AsyncSession(engine()) as session:
        stmt = (
            select(Visitor)
            .where(Visitor.plate == plate)
            .order_by(Visitor.started_at.desc())
            .limit(limit)
        )
        rows = (await session.exec(stmt)).all()
    return [
        VisitorInfo(
            plate=r.plate,
            company=r.company,
            reason=r.reason,
            contact_name=r.contact_name,
            phone=r.phone,
            duration=r.duration,
            is_returning=r.is_returning,
            call_sid=r.call_sid,
            started_at=r.started_at,
            ended_at=r.ended_at,
            duration_seconds=r.duration_seconds,
        )
        for r in rows
    ]


async def save_visit(info: VisitorInfo) -> int:
    async with AsyncSession(engine()) as session:
        row = Visitor(
            plate=info.plate or "UNKNOWN",
            company=info.company,
            reason=info.reason,
            contact_name=info.contact_name,
            phone=info.phone,
            duration=info.duration,
            is_returning=info.is_returning,
            call_sid=info.call_sid,
            started_at=info.started_at or datetime.utcnow(),
            ended_at=info.ended_at or datetime.utcnow(),
            duration_seconds=info.duration_seconds,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row.id or 0


async def save_conversation(
    call_sid: str,
    transcript: str,
    final_card: Optional[str] = None,
    plate: Optional[str] = None,
) -> None:
    async with AsyncSession(engine()) as session:
        row = Conversation(
            call_sid=call_sid,
            transcript=transcript,
            final_card=final_card,
            plate=plate,
        )
        session.add(row)
        await session.commit()
