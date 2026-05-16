"""Webhook delivery audit log."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import auth_utils, models
from ..db import get_db


router = APIRouter(prefix="/webhooks", tags=["webhooks"])


class DeliveryItem(BaseModel):
    id: int
    job_id: str
    event: str
    url: str
    http_status: int | None
    attempt: int
    delivered_at: datetime
    response_excerpt: str | None
    error: str | None


class DeliveryListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[DeliveryItem]


@router.get(
    "/deliveries",
    response_model=DeliveryListResponse,
    summary="Webhook delivery audit log for the current team",
)
async def list_deliveries(
    job_id: str | None = Query(default=None, min_length=1, max_length=64),
    event: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: models.User = Depends(auth_utils.get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DeliveryListResponse:
    base = select(models.WebhookDelivery).where(
        models.WebhookDelivery.team_id == user.team_id,
    )
    if job_id is not None:
        base = base.where(models.WebhookDelivery.job_id == job_id)
    if event is not None:
        base = base.where(models.WebhookDelivery.event == event)

    total_q = select(func.count()).select_from(base.subquery())
    total = (await db.execute(total_q)).scalar_one()

    page_q = (
        base
        .order_by(models.WebhookDelivery.delivered_at.desc())
        .limit(limit)
        .offset(offset)
    )
    page = await db.execute(page_q)
    items = [
        DeliveryItem(
            id=d.id, job_id=d.job_id, event=d.event, url=d.url,
            http_status=d.http_status, attempt=d.attempt,
            delivered_at=d.delivered_at, response_excerpt=d.response_excerpt,
            error=d.error,
        )
        for d in page.scalars()
    ]
    return DeliveryListResponse(total=total, limit=limit, offset=offset, items=items)
