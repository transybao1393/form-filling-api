"""Billing adapters and plan catalog.

Two provider routes:
- PayOS  — Vietnam customers (VND, bank transfer / QR / domestic cards)
- Payhip — international (USD, card / PayPal)

The catalog below is the canonical price list; provider price IDs are
resolved at checkout time from the env-configured product links.
"""

from __future__ import annotations

from typing import TypedDict


class PlanDef(TypedDict):
    name: str
    monthly_usd: int
    monthly_vnd: int
    jobs_per_month: int
    fills_per_month: int
    description: str


PLANS: dict[str, PlanDef] = {
    "free": {
        "name": "Free",
        "monthly_usd": 0,
        "monthly_vnd": 0,
        "jobs_per_month": 25,
        "fills_per_month": 200,
        "description": "Hobby use. Best for trying the API.",
    },
    "pro": {
        "name": "Pro",
        "monthly_usd": 49,
        "monthly_vnd": 1_190_000,
        "jobs_per_month": 500,
        "fills_per_month": 5_000,
        "description": "Small teams. Webhook deliveries log + templates.",
    },
    "scale": {
        "name": "Scale",
        "monthly_usd": 199,
        "monthly_vnd": 4_790_000,
        "jobs_per_month": 5_000,
        "fills_per_month": 50_000,
        "description": "Production workloads. Priority support.",
    },
}


def plan(plan_id: str) -> PlanDef | None:
    return PLANS.get(plan_id)
