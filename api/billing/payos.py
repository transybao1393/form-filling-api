"""PayOS adapter — create payment links + verify webhook signatures.

PayOS docs: https://payos.vn/docs

The signature is HMAC-SHA256 over a sorted "key=value&..." query-string of
specific fields (camelCase keys, sorted alphabetically). For payment
requests, the signed fields are: amount, cancelUrl, description, orderCode,
returnUrl. For webhooks, the body is `{ "data": {...}, "signature": "..." }`
and the signature is computed over the same sorted-query-string of all
fields in `data`.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

import httpx

from .. import config


log = logging.getLogger("api.billing.payos")


class PayOSError(RuntimeError):
    """Raised when PayOS is unconfigured or rejects a request."""


def _signed_string(fields: dict[str, Any]) -> str:
    parts = []
    for key in sorted(fields.keys()):
        value = fields[key]
        if value is None:
            value = ""
        parts.append(f"{key}={value}")
    return "&".join(parts)


def _sign(payload: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _require_credentials() -> tuple[str, str, str]:
    if not (config.PAYOS_CLIENT_ID and config.PAYOS_API_KEY and config.PAYOS_CHECKSUM_KEY):
        raise PayOSError(
            "PayOS not configured — set PAYOS_CLIENT_ID, PAYOS_API_KEY, "
            "PAYOS_CHECKSUM_KEY"
        )
    return config.PAYOS_CLIENT_ID, config.PAYOS_API_KEY, config.PAYOS_CHECKSUM_KEY


async def create_payment_link(
    *,
    order_code: int,
    amount_vnd: int,
    description: str,
    return_url: str,
    cancel_url: str,
    buyer_email: str | None = None,
) -> dict[str, Any]:
    """Create a hosted-checkout payment link and return PayOS's response.

    Response shape (from PayOS docs):
        {
          "code": "00",
          "desc": "success",
          "data": {
            "checkoutUrl": "...",
            "qrCode": "...",
            "orderCode": <int>,
            ...
          },
          "signature": "..."
        }
    """
    client_id, api_key, checksum_key = _require_credentials()
    signed_fields = {
        "amount": amount_vnd,
        "cancelUrl": cancel_url,
        "description": description,
        "orderCode": order_code,
        "returnUrl": return_url,
    }
    signature = _sign(_signed_string(signed_fields), checksum_key)

    body: dict[str, Any] = {
        **signed_fields,
        "signature": signature,
        "items": [
            {"name": description, "quantity": 1, "price": amount_vnd},
        ],
    }
    if buyer_email:
        body["buyerEmail"] = buyer_email

    headers = {
        "x-client-id": client_id,
        "x-api-key": api_key,
        "Content-Type": "application/json",
    }
    url = f"{config.PAYOS_API_BASE}/v2/payment-requests"

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(url, headers=headers, json=body)
    if resp.status_code >= 400:
        log.warning("payos: create_payment_link http=%d body=%s", resp.status_code, resp.text[:300])
        raise PayOSError(
            f"PayOS returned {resp.status_code}: {resp.text[:200]}"
        )
    payload = resp.json()
    if payload.get("code") not in ("00", 0, "0"):
        raise PayOSError(
            f"PayOS error {payload.get('code')!r}: {payload.get('desc')!r}"
        )
    return payload["data"]


def verify_webhook_signature(body: dict[str, Any]) -> bool:
    """Validate a PayOS webhook payload's signature against the checksum key.

    PayOS posts `{ "data": {...}, "signature": "..." }`; the signature is
    HMAC-SHA256 over the sorted key=value& string of `data`.
    """
    if not config.PAYOS_CHECKSUM_KEY:
        return False
    data = body.get("data")
    signature = body.get("signature")
    if not isinstance(data, dict) or not isinstance(signature, str):
        return False
    expected = _sign(_signed_string(data), config.PAYOS_CHECKSUM_KEY)
    return hmac.compare_digest(expected, signature)
