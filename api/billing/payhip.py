"""Payhip adapter — webhook signature verification + sale parsing.

Payhip's webhook format has shifted over the years; current accounts can
sign with either an MD5(api_secret + body) digest sent as a `signature`
form field, or an HMAC-SHA256 of the raw body sent in an
`X-Payhip-Signature` header. We accept both — the operator picks one in
their Payhip dashboard and configures PAYHIP_WEBHOOK_SECRET to match.

Webhook events we care about:
- `paid`              — one-time purchase
- `subscription_paid` — recurring payment success
- `subscription_canceled` — cancel
"""

from __future__ import annotations

import hashlib
import hmac
import logging

from .. import config


log = logging.getLogger("api.billing.payhip")


def verify_webhook(
    *,
    raw_body: bytes,
    form_signature: str | None,
    header_signature: str | None,
) -> bool:
    """Return True if either signature (form-field or header) matches.

    - form_signature: MD5(secret + body_string) hex — Payhip legacy mode.
    - header_signature: HMAC-SHA256(secret, body) hex — Payhip "Verify
      signature" toggle on newer dashboards. Header is `X-Payhip-Signature`.

    Returns False if no signature is provided OR no secret is configured.
    """
    secret = config.PAYHIP_WEBHOOK_SECRET
    if not secret:
        return False

    if header_signature:
        expected = hmac.new(
            secret.encode("utf-8"), raw_body, hashlib.sha256,
        ).hexdigest()
        # Allow either "sha256=<hex>" or bare hex in the header.
        candidate = header_signature.strip()
        if candidate.lower().startswith("sha256="):
            candidate = candidate.split("=", 1)[1]
        if hmac.compare_digest(expected, candidate):
            return True

    if form_signature:
        h = hashlib.md5()
        h.update(secret.encode("utf-8"))
        h.update(raw_body)
        if hmac.compare_digest(h.hexdigest(), form_signature.strip()):
            return True

    return False


def plan_from_product(product_link: str | None) -> str:
    """Map a Payhip product link (or product id) to one of our plan ids.

    Compares against config.PAYHIP_PRODUCT_LINK_PRO / _SCALE so the operator
    keeps mapping in env. Falls back to 'pro' on unknown product (better to
    upgrade a customer than leave them stuck on free after a successful
    charge).
    """
    if not product_link:
        return "pro"
    pl = product_link.strip().rstrip("/")
    if config.PAYHIP_PRODUCT_LINK_SCALE and pl in config.PAYHIP_PRODUCT_LINK_SCALE:
        return "scale"
    if config.PAYHIP_PRODUCT_LINK_PRO and pl in config.PAYHIP_PRODUCT_LINK_PRO:
        return "pro"
    if "scale" in pl.lower():
        return "scale"
    return "pro"
