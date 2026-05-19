"""Thin async client for the host-native llm_service.

Replaces the old direct Ollama client. All LLM-orchestration lives on the
other side of this HTTP boundary; this module only knows how to POST a
generation request and surface failures.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from . import config


log = logging.getLogger("api.llm_service")


class LLMServiceError(RuntimeError):
    pass


async def generate(
    questionnaire_text: str,
    references: list[tuple[str, str]],
    questionnaire_title: str | None = None,
) -> dict[str, Any]:
    """POST /generate on the llm_service. Returns the `data` dict
    (already validated + normalized service-side)."""
    body = {
        "questionnaire_text": questionnaire_text,
        "references": [{"filename": n, "text": t} for n, t in references],
        "questionnaire_title": questionnaire_title,
    }
    url = f"{config.LLM_SERVICE_URL}/generate"
    log.info("llm_service: POST %s refs=%d", url, len(references))
    try:
        async with httpx.AsyncClient(timeout=config.LLM_SERVICE_TIMEOUT) as client:
            r = await client.post(url, json=body)
    except httpx.HTTPError as e:
        raise LLMServiceError(
            f"failed to reach llm_service at {url}: "
            f"{type(e).__name__}: {e or '(no message)'}"
        ) from e

    if r.status_code != 200:
        raise LLMServiceError(
            f"llm_service returned {r.status_code}: {r.text[:500]}"
        )

    payload = r.json()
    data = payload.get("data")
    if not isinstance(data, dict):
        raise LLMServiceError(
            f"unexpected llm_service payload (no `data` object): "
            f"{str(payload)[:500]}"
        )
    return data


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
