"""Per-team usage metering.

One `UsageRecord` row per (team_id, calendar month UTC) tracks counts the
billing layer enforces against the team's plan limits. Helpers open their
own DB sessions so they can be called from any context (FastAPI route, arq
worker, ad-hoc script).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import billing, models
from .db import get_sessionmaker


log = logging.getLogger("api.usage")


def _period_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    now = now or datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


async def get_or_create_period(
    db: AsyncSession, team_id: int,
) -> models.UsageRecord:
    start, end = _period_bounds()
    result = await db.execute(
        select(models.UsageRecord).where(
            models.UsageRecord.team_id == team_id,
            models.UsageRecord.period_start == start,
        )
    )
    rec = result.scalar_one_or_none()
    if rec is None:
        now = datetime.now(timezone.utc)
        rec = models.UsageRecord(
            team_id=team_id, period_start=start, period_end=end,
            jobs_count=0, fills_count=0, llm_tokens=0, storage_bytes=0,
            updated_at=now,
        )
        db.add(rec)
        await db.commit()
        await db.refresh(rec)
    return rec


async def increment(team_id: int | None, **deltas: int) -> None:
    """Best-effort increment. No-op if team_id is None or DB unreachable."""
    if team_id is None:
        return
    try:
        sm = get_sessionmaker()
        async with sm() as session:
            rec = await get_or_create_period(session, team_id)
            for k, v in deltas.items():
                if v == 0:
                    continue
                setattr(rec, k, (getattr(rec, k, 0) or 0) + v)
            rec.updated_at = datetime.now(timezone.utc)
            await session.commit()
    except Exception as e:
        log.warning("usage.increment: team_id=%s deltas=%s failed: %s", team_id, deltas, e)


async def current_plan(db: AsyncSession, team_id: int) -> str:
    result = await db.execute(
        select(models.Subscription).where(models.Subscription.team_id == team_id)
    )
    sub = result.scalar_one_or_none()
    return sub.plan if sub is not None else "free"


async def check_limit(
    db: AsyncSession, team_id: int, metric: str,
) -> tuple[bool, int, int]:
    """Return (within_limit, used, cap). metric in {'jobs', 'fills'}.

    cap == -1 means "no enforced limit" (free plans still have one; this is
    only used if an admin assigns an unknown plan id)."""
    plan_id = await current_plan(db, team_id)
    plan_def = billing.plan(plan_id) or billing.plan("free")
    cap_key = f"{metric}_per_month"
    cap = plan_def.get(cap_key) if plan_def else None
    if cap is None:
        return True, 0, -1
    rec = await get_or_create_period(db, team_id)
    used_key = f"{metric}_count"
    used = getattr(rec, used_key, 0) or 0
    return (used < cap), used, cap
