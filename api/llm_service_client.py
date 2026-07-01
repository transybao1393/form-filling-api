"""Thin async client for the host-native llm_service.

Replaces the old direct Ollama client. All LLM-orchestration lives on the
other side of this HTTP boundary; this module only knows how to POST a
generation request and surface failures.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

import httpx

from . import config


log = logging.getLogger("api.llm_service")


class LLMServiceError(RuntimeError):
    """Base error for all llm_service call failures."""


class LLMServiceUnavailable(LLMServiceError):
    """The llm_service itself is unreachable (network error, connection
    refused). Not retryable from the client side — the operator needs to
    start the service. Caller surfaces a clear message to the job."""


class LLMServiceUpstreamError(LLMServiceError):
    """The llm_service answered, but its upstream (Ollama) failed —
    typically a ReadTimeout when Ollama was busy/cold. Retryable: a brief
    backoff often clears it."""


# How many times to retry a 502 (upstream Ollama hiccup) before giving up.
# 0 disables retries entirely. Default 2 → up to 3 attempts total.
_RETRY_ON_502 = int(os.getenv("LLM_SERVICE_RETRY_ON_502", "2"))
# Initial backoff between retries (seconds); doubles each attempt.
_RETRY_BACKOFF_S = float(os.getenv("LLM_SERVICE_RETRY_BACKOFF", "3"))


def _parse_upstream_detail(body_text: str) -> str:
    """llm_service emits errors as `{"detail": "..."}`. Pull just the
    detail string so the job error isn't nested JSON-in-a-string."""
    try:
        d = json.loads(body_text)
        if isinstance(d, dict) and isinstance(d.get("detail"), str):
            return d["detail"]
    except (ValueError, TypeError):
        pass
    return body_text[:500]


async def generate(
    questionnaire_text: str,
    references: list[tuple[str, str]],
    questionnaire_title: str | None = None,
) -> dict[str, Any]:
    """POST /generate on the llm_service. Returns the `data` dict
    (already validated + normalized service-side).

    Retries up to LLM_SERVICE_RETRY_ON_502 times on HTTP 502, which
    indicates the llm_service is up but its upstream Ollama timed out or
    hiccuped — a transient condition that usually clears on a second
    attempt. Other failures (network errors reaching llm_service, 4xx,
    other 5xx) are not retried.
    """
    body = {
        "questionnaire_text": questionnaire_text,
        "references": [{"filename": n, "text": t} for n, t in references],
        "questionnaire_title": questionnaire_title,
    }
    url = f"{config.LLM_SERVICE_URL}/generate"

    attempts = _RETRY_ON_502 + 1
    last_detail: str | None = None
    for attempt in range(1, attempts + 1):
        log.info(
            "llm_service: POST %s refs=%d (attempt %d/%d)",
            url, len(references), attempt, attempts,
        )
        try:
            async with httpx.AsyncClient(timeout=config.LLM_SERVICE_TIMEOUT) as client:
                r = await client.post(url, json=body)
        except httpx.HTTPError as e:
            # Network-level failure (DNS, connect-refused, ReadTimeout
            # on our own client). Don't retry — the service is down.
            raise LLMServiceUnavailable(
                f"could not reach llm_service at {url}: "
                f"{type(e).__name__}: {e or '(no message)'}"
            ) from e

        if r.status_code == 200:
            payload = r.json()
            data = payload.get("data")
            if not isinstance(data, dict):
                raise LLMServiceError(
                    f"unexpected llm_service payload (no `data` object): "
                    f"{str(payload)[:500]}"
                )
            return data

        if r.status_code == 502 and attempt < attempts:
            last_detail = _parse_upstream_detail(r.text)
            backoff = _RETRY_BACKOFF_S * (2 ** (attempt - 1))
            log.warning(
                "llm_service: 502 upstream hiccup (attempt %d/%d): %s "
                "— retrying in %.1fs",
                attempt, attempts, last_detail, backoff,
            )
            await asyncio.sleep(backoff)
            continue

        # 4xx, non-502 5xx, or 502 on the final attempt.
        detail = _parse_upstream_detail(r.text)
        if r.status_code == 502:
            raise LLMServiceUpstreamError(
                f"upstream LLM failed after {attempts} attempts: {detail}"
            )
        raise LLMServiceError(
            f"llm_service returned HTTP {r.status_code}: {detail}"
        )

    # Unreachable — the loop above always returns or raises — but mypy
    # likes a guard.
    raise LLMServiceUpstreamError(
        f"upstream LLM failed after {attempts} attempts: {last_detail or '(no detail)'}"
    )


async def extract_fields(
    questionnaire_text: str,
    questionnaire_title: str | None = None,
) -> dict[str, Any]:
    """POST /extract-fields on the llm_service. Returns the `data` dict."""
    body = {
        "questionnaire_text": questionnaire_text,
        "questionnaire_title": questionnaire_title,
    }
    url = f"{config.LLM_SERVICE_URL}/extract-fields"

    attempts = _RETRY_ON_502 + 1
    last_detail: str | None = None
    for attempt in range(1, attempts + 1):
        log.info(
            "llm_service: POST %s extract-fields (attempt %d/%d)",
            url, attempt, attempts,
        )
        try:
            async with httpx.AsyncClient(timeout=config.LLM_SERVICE_TIMEOUT) as client:
                r = await client.post(url, json=body)
        except httpx.HTTPError as e:
            raise LLMServiceUnavailable(
                f"could not reach llm_service at {url}: "
                f"{type(e).__name__}: {e or '(no message)'}"
            ) from e

        if r.status_code == 200:
            payload = r.json()
            data = payload.get("data")
            if not isinstance(data, dict):
                raise LLMServiceError(
                    f"unexpected llm_service payload (no `data` object): "
                    f"{str(payload)[:500]}"
                )
            return data

        if r.status_code == 502 and attempt < attempts:
            last_detail = _parse_upstream_detail(r.text)
            backoff = _RETRY_BACKOFF_S * (2 ** (attempt - 1))
            log.warning(
                "llm_service: 502 upstream hiccup (attempt %d/%d): %s "
                "— retrying in %.1fs",
                attempt, attempts, last_detail, backoff,
            )
            await asyncio.sleep(backoff)
            continue

        detail = _parse_upstream_detail(r.text)
        if r.status_code == 502:
            raise LLMServiceUpstreamError(
                f"upstream LLM failed after {attempts} attempts: {detail}"
            )
        raise LLMServiceError(
            f"llm_service returned HTTP {r.status_code}: {detail}"
        )

    raise LLMServiceUpstreamError(
        f"upstream LLM failed after {attempts} attempts: {last_detail or '(no detail)'}"
    )


async def health() -> dict[str, Any]:
    """Probe llm_service /healthz. Returns the parsed body on success, or
    a synthetic `{"ollama": "down", "model": ""}` shape on any failure so
    callers don't have to handle exceptions."""
    url = f"{config.LLM_SERVICE_URL}/healthz"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(url)
        if r.status_code != 200:
            return {"ollama": "down", "model": ""}
        body = r.json()
        if not isinstance(body, dict):
            return {"ollama": "down", "model": ""}
        return body
    except (httpx.HTTPError, ValueError):
        return {"ollama": "down", "model": ""}
