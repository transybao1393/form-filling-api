"""Billing endpoints: plan info, invoices, region-aware checkout, webhooks.

Region detection: callers can override via the `region` body field. Default
is "international" (Payhip). PayOS handles VN. Phase 7 wires Payhip; this
module exposes the routes today and raises 503 for unconfigured providers.
"""

from __future__ import annotations

import logging
import secrets
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .. import auth_utils, billing, config, models, usage
from ..billing import payhip, payos
from ..db import get_db


log = logging.getLogger("api.billing")

router = APIRouter(tags=["billing"])


# --- Schemas ---------------------------------------------------------------

class PlanResponse(BaseModel):
    id: str
    name: str
    monthly_usd: int
    monthly_vnd: int
    jobs_per_month: int
    fills_per_month: int
    description: str


class PlansListResponse(BaseModel):
    plans: list[PlanResponse]


class SubscriptionResponse(BaseModel):
    plan: str
    plan_name: str
    status: str
    provider: str | None
    current_period_start: datetime | None
    current_period_end: datetime | None


class CheckoutRequest(BaseModel):
    plan: str = Field(..., description="Plan id (e.g. 'pro', 'scale')")
    region: str = Field(
        default="international",
        description="'vn' → PayOS; anything else → Payhip",
    )


class CheckoutResponse(BaseModel):
    provider: str
    checkout_url: str
    order_code: int | None = None


class InvoiceResponse(BaseModel):
    id: int
    number: str
    provider: str
    amount_cents: int
    currency: str
    status: str
    description: str | None
    paid_at: datetime | None
    created_at: datetime


class UsageResponse(BaseModel):
    period_start: datetime
    period_end: datetime
    plan: str
    jobs_used: int
    jobs_cap: int
    fills_used: int
    fills_cap: int
    storage_bytes: int


# --- Helpers ---------------------------------------------------------------

async def _get_or_create_subscription(
    db: AsyncSession, team_id: int,
) -> models.Subscription:
    """Upsert. Subscription.team_id is unique, so two concurrent first
    callers race on INSERT — we catch the IntegrityError, rollback, and
    re-read the winning row."""
    result = await db.execute(
        select(models.Subscription).where(models.Subscription.team_id == team_id)
    )
    sub = result.scalar_one_or_none()
    if sub is not None:
        return sub
    now = datetime.now(timezone.utc)
    sub = models.Subscription(
        team_id=team_id, plan="free", status="active",
        created_at=now, updated_at=now,
    )
    db.add(sub)
    try:
        await db.commit()
        await db.refresh(sub)
        return sub
    except IntegrityError:
        await db.rollback()
        result = await db.execute(
            select(models.Subscription).where(models.Subscription.team_id == team_id)
        )
        winner = result.scalar_one_or_none()
        if winner is None:
            # Genuine unrelated constraint failure — re-raise so the caller sees it.
            raise
        return winner


def _plan_response(plan_id: str) -> PlanResponse:
    p = billing.plan(plan_id)
    if p is None:
        raise HTTPException(status_code=404, detail=f"unknown plan {plan_id!r}")
    return PlanResponse(id=plan_id, **p)


# --- Plan catalog (public) -------------------------------------------------

@router.get(
    "/billing/plans",
    response_model=PlansListResponse,
    summary="List all available plans",
)
async def list_plans() -> PlansListResponse:
    return PlansListResponse(
        plans=[_plan_response(pid) for pid in billing.PLANS.keys()],
    )


# --- Current plan + invoices ----------------------------------------------

@router.get(
    "/billing/plan",
    response_model=SubscriptionResponse,
    summary="Current team's subscription",
)
async def get_subscription(
    user: models.User = Depends(auth_utils.get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SubscriptionResponse:
    sub = await _get_or_create_subscription(db, user.team_id)
    plan_meta = billing.plan(sub.plan) or {"name": sub.plan}
    return SubscriptionResponse(
        plan=sub.plan,
        plan_name=plan_meta["name"],
        status=sub.status,
        provider=sub.provider,
        current_period_start=sub.current_period_start,
        current_period_end=sub.current_period_end,
    )


@router.get(
    "/billing/invoices",
    response_model=list[InvoiceResponse],
    summary="Invoices for the current team (newest first)",
)
async def list_invoices(
    limit: int = Query(default=50, ge=1, le=200),
    user: models.User = Depends(auth_utils.get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[InvoiceResponse]:
    result = await db.execute(
        select(models.Invoice)
        .where(models.Invoice.team_id == user.team_id)
        .order_by(models.Invoice.created_at.desc())
        .limit(limit)
    )
    return [
        InvoiceResponse(
            id=i.id, number=i.number, provider=i.provider,
            amount_cents=i.amount_cents, currency=i.currency,
            status=i.status, description=i.description,
            paid_at=i.paid_at, created_at=i.created_at,
        )
        for i in result.scalars()
    ]


# --- Usage rollup ----------------------------------------------------------

@router.get(
    "/billing/usage",
    response_model=UsageResponse,
    summary="Current calendar-month usage for the team",
)
async def get_usage(
    user: models.User = Depends(auth_utils.get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UsageResponse:
    plan_id = await usage.current_plan(db, user.team_id)
    rec = await usage.get_or_create_period(db, user.team_id)
    plan_def = billing.plan(plan_id) or billing.plan("free")
    return UsageResponse(
        period_start=rec.period_start,
        period_end=rec.period_end,
        plan=plan_id,
        jobs_used=rec.jobs_count,
        jobs_cap=plan_def["jobs_per_month"] if plan_def else 0,
        fills_used=rec.fills_count,
        fills_cap=plan_def["fills_per_month"] if plan_def else 0,
        storage_bytes=rec.storage_bytes,
    )


# --- Checkout (region-aware) ----------------------------------------------

@router.post(
    "/billing/checkout",
    response_model=CheckoutResponse,
    summary="Start checkout for a plan (provider chosen by region)",
)
async def start_checkout(
    body: CheckoutRequest,
    user: models.User = Depends(auth_utils.get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CheckoutResponse:
    plan_def = billing.plan(body.plan)
    if plan_def is None:
        raise HTTPException(status_code=400, detail=f"unknown plan {body.plan!r}")
    if body.plan == "free":
        # Free doesn't need checkout — just upgrade in place.
        sub = await _get_or_create_subscription(db, user.team_id)
        sub.plan = "free"
        sub.status = "active"
        sub.provider = None
        sub.updated_at = datetime.now(timezone.utc)
        await db.commit()
        return CheckoutResponse(provider="none", checkout_url="")

    if body.region.lower() in ("vn", "vietnam"):
        return await _checkout_payos(body.plan, plan_def, user, db)
    return await _checkout_payhip(body.plan, plan_def, user)


async def _checkout_payos(
    plan_id: str, plan_def: dict, user: models.User, db: AsyncSession,
) -> CheckoutResponse:
    if not (
        config.PAYOS_CLIENT_ID and config.PAYOS_API_KEY and config.PAYOS_CHECKSUM_KEY
    ):
        raise HTTPException(
            status_code=503,
            detail="PayOS not configured on this server",
        )
    amount = plan_def["monthly_vnd"]
    # PayOS orderCode <= 9_007_199_254_740_991 — pack 4+4+3 digits (time/team/random).
    order_code = int(
        f"{int(time.time()) % 10_000:04d}"
        f"{user.team_id % 10_000:04d}"
        f"{secrets.randbelow(1000):03d}"
    )
    description = f"{plan_def['name']} — team {user.team_id}"

    invoice = models.Invoice(
        team_id=user.team_id,
        number=f"PAYOS-{order_code}",
        provider="payos",
        external_id=str(order_code),
        amount_cents=amount,  # VND has no cents; this column stores the full amount
        currency="VND",
        status="pending",
        description=description,
        created_at=datetime.now(timezone.utc),
    )
    db.add(invoice)
    await db.commit()

    try:
        data = await payos.create_payment_link(
            order_code=order_code,
            amount_vnd=amount,
            description=description,
            return_url=f"{config.APP_BASE_URL}/billing/return?provider=payos&order={order_code}",
            cancel_url=f"{config.APP_BASE_URL}/billing/cancel?provider=payos&order={order_code}",
            buyer_email=user.email,
        )
    except payos.PayOSError as e:
        log.warning("checkout_payos: %s", e)
        raise HTTPException(status_code=502, detail=f"PayOS rejected: {e}")
    return CheckoutResponse(
        provider="payos",
        checkout_url=data.get("checkoutUrl") or "",
        order_code=order_code,
    )


async def _checkout_payhip(
    plan_id: str, plan_def: dict, user: models.User,
) -> CheckoutResponse:
    # Phase 7 fills this in — for now, return the env-configured product
    # link or a 503 if unset.
    link_map = {
        "pro": config.PAYHIP_PRODUCT_LINK_PRO,
        "scale": config.PAYHIP_PRODUCT_LINK_SCALE,
    }
    link = link_map.get(plan_id)
    if not link:
        raise HTTPException(
            status_code=503,
            detail=f"Payhip product link for {plan_id!r} not configured",
        )
    # Pass the team_id so the webhook can attribute the sale; supported via
    # ?metadata in Payhip's checkout query string.
    checkout = f"{link}?metadata%5Bteam_id%5D={user.team_id}"
    return CheckoutResponse(provider="payhip", checkout_url=checkout)


# --- PayOS webhook ---------------------------------------------------------

@router.post(
    "/webhooks/payos",
    summary="PayOS payment webhook (signature-verified)",
    include_in_schema=True,
)
async def payos_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    body = await request.json()
    if not payos.verify_webhook_signature(body):
        raise HTTPException(status_code=401, detail="invalid signature")
    data = body.get("data") or {}
    order_code = str(data.get("orderCode"))
    if not order_code:
        raise HTTPException(status_code=400, detail="missing orderCode")

    result = await db.execute(
        select(models.Invoice).where(
            models.Invoice.provider == "payos",
            models.Invoice.external_id == order_code,
        )
    )
    invoice = result.scalar_one_or_none()
    if invoice is None:
        log.warning("payos_webhook: no invoice for orderCode=%s", order_code)
        # Acknowledge so PayOS doesn't keep retrying. Possibly a test ping.
        return {"received": True, "matched": False}

    now = datetime.now(timezone.utc)
    if str(data.get("code")) in ("00", "0"):
        # Idempotency: PayOS retries on 5xx (and on any timeout) so the same
        # webhook can arrive multiple times. If we already marked this
        # invoice paid, just ack — re-running the subscription update below
        # would re-extend current_period_end on every retry.
        if invoice.status == "paid":
            return {"received": True, "duplicate": True, "invoice_status": "paid"}
        invoice.status = "paid"
        invoice.paid_at = now

        sub_q = await db.execute(
            select(models.Subscription).where(
                models.Subscription.team_id == invoice.team_id
            )
        )
        sub = sub_q.scalar_one_or_none()
        plan_id = _plan_from_description(invoice.description)
        if sub is None:
            sub = models.Subscription(
                team_id=invoice.team_id,
                plan=plan_id,
                status="active",
                provider="payos",
                external_id=order_code,
                created_at=now,
                updated_at=now,
            )
            db.add(sub)
        else:
            sub.plan = plan_id
            sub.status = "active"
            sub.provider = "payos"
            sub.external_id = order_code
            sub.updated_at = now
        # 30-day rolling period (PayOS is one-shot; cron would renew).
        sub.current_period_start = now
        sub.current_period_end = now.replace(microsecond=0) + _month()
    else:
        invoice.status = "failed"
    await db.commit()
    return {"received": True, "matched": True, "invoice_status": invoice.status}


def _month():
    from datetime import timedelta
    return timedelta(days=30)


def _plan_from_description(desc: str | None) -> str:
    if not desc:
        return "pro"
    low = desc.lower()
    if "scale" in low:
        return "scale"
    if "pro" in low:
        return "pro"
    return "pro"


# --- Payhip webhook --------------------------------------------------------

@router.post(
    "/webhooks/payhip",
    summary="Payhip payment webhook (signature-verified)",
    include_in_schema=True,
)
async def payhip_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    raw_body = await request.body()
    # Payhip posts application/x-www-form-urlencoded by default.
    form = await request.form()
    form_signature = form.get("signature")
    header_signature = request.headers.get("x-payhip-signature")
    if not payhip.verify_webhook(
        raw_body=raw_body,
        form_signature=form_signature if isinstance(form_signature, str) else None,
        header_signature=header_signature,
    ):
        raise HTTPException(status_code=401, detail="invalid signature")

    event_type = form.get("type") or form.get("event") or "paid"
    team_id_raw = form.get("metadata[team_id]") or form.get("team_id")
    try:
        team_id = int(team_id_raw) if team_id_raw else None
    except (TypeError, ValueError):
        team_id = None
    if team_id is None:
        log.warning("payhip_webhook: missing/invalid team_id metadata; event=%s", event_type)
        return {"received": True, "matched": False, "reason": "missing team_id"}

    team = await db.get(models.Team, team_id)
    if team is None:
        log.warning("payhip_webhook: team_id=%s not found", team_id)
        return {"received": True, "matched": False, "reason": "unknown team_id"}

    transaction_id = (
        form.get("transaction_id") or form.get("subscription_id") or
        form.get("payment_id") or secrets.token_hex(8)
    )
    product_link = form.get("product_link") or form.get("product_url")
    plan_id = payhip.plan_from_product(product_link)
    amount_raw = form.get("price") or form.get("amount") or "0"
    try:
        amount_cents = int(float(amount_raw) * 100)
    except ValueError:
        amount_cents = 0
    currency = (form.get("currency") or "USD").upper()
    now = datetime.now(timezone.utc)

    if event_type in ("paid", "subscription_paid"):
        # Idempotency: Payhip may retry on timeout, so the same
        # transaction_id can arrive twice. Skip if we've already recorded it.
        existing_q = await db.execute(
            select(models.Invoice).where(
                models.Invoice.provider == "payhip",
                models.Invoice.external_id == str(transaction_id),
            )
        )
        if existing_q.scalar_one_or_none() is not None:
            return {
                "received": True, "duplicate": True,
                "event": event_type, "transaction_id": str(transaction_id),
            }

        invoice = models.Invoice(
            team_id=team_id,
            number=f"PAYHIP-{transaction_id}",
            provider="payhip",
            external_id=str(transaction_id),
            amount_cents=amount_cents,
            currency=currency,
            status="paid",
            description=f"Payhip {event_type}",
            paid_at=now,
            created_at=now,
        )
        db.add(invoice)

        sub_q = await db.execute(
            select(models.Subscription).where(models.Subscription.team_id == team_id)
        )
        sub = sub_q.scalar_one_or_none()
        if sub is None:
            sub = models.Subscription(
                team_id=team_id, plan=plan_id, status="active",
                provider="payhip", external_id=str(transaction_id),
                current_period_start=now,
                current_period_end=now + _month(),
                created_at=now, updated_at=now,
            )
            db.add(sub)
        else:
            sub.plan = plan_id
            sub.status = "active"
            sub.provider = "payhip"
            sub.external_id = str(transaction_id)
            sub.current_period_start = now
            sub.current_period_end = now + _month()
            sub.updated_at = now
    elif event_type in ("subscription_canceled", "refunded"):
        sub_q = await db.execute(
            select(models.Subscription).where(models.Subscription.team_id == team_id)
        )
        sub = sub_q.scalar_one_or_none()
        if sub is not None:
            sub.status = "canceled" if event_type == "subscription_canceled" else "refunded"
            sub.updated_at = now
    else:
        log.info("payhip_webhook: ignoring event_type=%s for team_id=%s", event_type, team_id)
        return {"received": True, "matched": True, "ignored": True}

    await db.commit()
    return {"received": True, "matched": True, "event": event_type}
