"""
SQL helpers for the guard query API.

We use raw SQL (not SQLModel) here for the analytics queries because they're
trivially expressed in SQL and we want full control over the aggregates.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import text
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import engine


async def list_recent(
    *, plate: Optional[str] = None, days: int = 7, limit: int = 50
) -> list[dict]:
    where = "started_at >= :since"
    params: dict = {"since": datetime.utcnow() - timedelta(days=days), "limit": limit}
    if plate:
        where += " AND plate = :plate"
        params["plate"] = plate.upper()
    sql = text(
        f"""
        SELECT id, plate, company, reason, contact_name, phone, duration,
               is_returning, call_sid, started_at, ended_at, duration_seconds
        FROM visitors
        WHERE {where}
        ORDER BY started_at DESC
        LIMIT :limit
        """
    )
    async with AsyncSession(engine()) as session:
        rows = (await session.exec(sql, params=params)).mappings().all()
    out = []
    for r in rows:
        # SQLite returns datetimes as plain strings, so guard against both
        # datetime and str before calling .isoformat().
        def _iso(v):
            if v is None:
                return None
            if hasattr(v, "isoformat"):
                return v.isoformat()
            return str(v)
        out.append(
            {
                "id": r["id"],
                "plate": r["plate"],
                "company": r["company"],
                "reason": r["reason"],
                "contact_name": r["contact_name"],
                "phone": r["phone"],
                "duration": r["duration"],
                "is_returning": bool(r["is_returning"]),
                "call_sid": r["call_sid"],
                "started_at": _iso(r["started_at"]),
                "ended_at": _iso(r["ended_at"]),
                "duration_seconds": r["duration_seconds"],
            }
        )
    return out


async def stats(*, days: int = 7) -> dict:
    """Quick statistics used by the guard agent and the dashboard."""
    since = datetime.utcnow() - timedelta(days=days)
    async with AsyncSession(engine()) as session:
        # total + unique plates
        total = (
            await session.exec(
                text("SELECT COUNT(*) AS c FROM visitors WHERE started_at >= :s"), params={"s": since})
        ).first()
        unique = (
            await session.exec(
                text("SELECT COUNT(DISTINCT plate) AS c FROM visitors WHERE started_at >= :s"), params={"s": since})
        ).first()
        # by hour of day (SQLite strftime)
        by_hour = (
            await session.exec(
                text(
                    """
                    SELECT CAST(strftime('%H', started_at) AS INTEGER) AS hour, COUNT(*) AS c
                    FROM visitors
                    WHERE started_at >= :s
                    GROUP BY hour
                    ORDER BY hour
                    """
                ), params={"s": since})
        ).all()
        # by reason (top 5)
        by_reason = (
            await session.exec(
                text(
                    """
                    SELECT COALESCE(reason, '未知') AS reason, COUNT(*) AS c
                    FROM visitors
                    WHERE started_at >= :s
                    GROUP BY reason
                    ORDER BY c DESC
                    LIMIT 5
                    """
                ), params={"s": since})
        ).all()
        # by plate (top returning)
        by_plate = (
            await session.exec(
                text(
                    """
                    SELECT plate, COUNT(*) AS c
                    FROM visitors
                    WHERE started_at >= :s
                    GROUP BY plate
                    ORDER BY c DESC
                    LIMIT 10
                    """
                ), params={"s": since})
        ).all()
    peak_hour = max(by_hour, key=lambda r: r[1], default=(None, 0))
    return {
        "days": days,
        "total_visits": int(total[0] or 0),
        "unique_plates": int(unique[0] or 0),
        "peak_hour": {"hour": peak_hour[0], "visits": int(peak_hour[1] or 0)},
        "by_hour": [{"hour": h, "visits": int(c)} for h, c in by_hour],
        "by_reason": [{"reason": r, "visits": int(c)} for r, c in by_reason],
        "top_plates": [{"plate": p, "visits": int(c)} for p, c in by_plate],
    }


async def plate_history(plate: str, *, days: int = 90) -> list[dict]:
    since = datetime.utcnow() - timedelta(days=days)
    sql = text(
        """
        SELECT id, plate, company, reason, contact_name, phone, duration,
               is_returning, call_sid, started_at, duration_seconds
        FROM visitors
        WHERE plate = :plate AND started_at >= :s
        ORDER BY started_at DESC
        """
    )
    async with AsyncSession(engine()) as session:
        rows = (await session.exec(sql, params={"plate": plate.upper(), "s": since})).mappings().all()
    return [dict(r) for r in rows]
