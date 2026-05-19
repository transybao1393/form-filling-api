"""Thin async client around Ollama's /api/chat endpoint."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from . import config


log = logging.getLogger("llm_service.ollama")


class OllamaError(RuntimeError):
    pass


async def chat(
    messages: list[dict[str, str]],
    *,
    json_format: bool = True,
) -> str:
    """
    Call Ollama /api/chat and return the assistant's `message.content`.

    `think: false` disables Qwen3's reasoning trace on Ollama 0.4+. We also
    append `/no_think` in the system prompt (see prompts.py) for older builds.
    """
    body: dict[str, Any] = {
        "model": config.OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
        "think": False,
        "options": {
            "temperature": config.OLLAMA_TEMPERATURE,
            "num_ctx": config.OLLAMA_NUM_CTX,
        },
    }
    if json_format:
        body["format"] = "json"

    url = f"{config.OLLAMA_URL}/api/chat"
    total_chars = sum(len(m.get("content", "")) for m in messages)
    log.info(
        "ollama: POST %s model=%s num_ctx=%d input_chars=%d (~%d tok)",
        url, config.OLLAMA_MODEL, config.OLLAMA_NUM_CTX,
        total_chars, total_chars // 4,
    )
    async with httpx.AsyncClient(timeout=config.OLLAMA_TIMEOUT) as client:
        try:
            r = await client.post(url, json=body)
        except httpx.HTTPError as e:
            # httpx.ReadTimeout/PoolTimeout often have empty str(e); include
            # the exception class so the failure mode is identifiable.
            raise OllamaError(
                f"failed to reach Ollama at {url}: "
                f"{type(e).__name__}: {e or '(no message)'}"
            ) from e

    if r.status_code != 200:
        raise OllamaError(
            f"Ollama returned {r.status_code}: {r.text[:500]}"
        )

    payload = r.json()
    _log_timings(payload)
    msg = payload.get("message") or {}
    content = msg.get("content")
    if not isinstance(content, str):
        raise OllamaError(f"unexpected Ollama payload: {json.dumps(payload)[:500]}")
    return content


def _log_timings(payload: dict[str, Any]) -> None:
    """Log Ollama's per-call timing breakdown so slow requests are diagnosable."""
    try:
        total = payload["total_duration"] / 1e9
        load = payload.get("load_duration", 0) / 1e9
        prompt_eval = payload.get("prompt_eval_duration", 0) / 1e9
        eval_dur = payload.get("eval_duration", 0) / 1e9
        prompt_tokens = payload.get("prompt_eval_count", 0)
        out_tokens = payload.get("eval_count", 0)
        tps = out_tokens / eval_dur if eval_dur else 0.0
        log.info(
            "ollama: total=%.2fs load=%.2fs prompt=%.2fs(%d tok) "
            "output=%.2fs(%d tok @ %.1f tok/s) num_ctx=%d",
            total, load, prompt_eval, prompt_tokens,
            eval_dur, out_tokens, tps, config.OLLAMA_NUM_CTX,
        )
    except (KeyError, TypeError, ZeroDivisionError):
        pass


async def health() -> bool:
    """True if Ollama answers /api/tags within timeout."""
    url = f"{config.OLLAMA_URL}/api/tags"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(url)
        return r.status_code == 200
    except httpx.HTTPError:
        return False
