"""Startup checks the LLM service owns:

  1. Is the `ollama` binary installed on this host?
  2. Is the Ollama daemon reachable?
  3. Is the configured model present? If not, pull it.

Called from main.py's FastAPI lifespan so the service refuses to come up
without an installed Ollama, and so the first request after a fresh
install is never the one that triggers a 5 GB model download.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys

import httpx

from . import config


log = logging.getLogger("llm_service.bootstrap")


class OllamaNotInstalled(RuntimeError):
    """Raised when the `ollama` binary isn't on PATH."""


def ensure_ollama_installed() -> str:
    """Return the path to the ollama binary, or raise if missing.

    Hard-fail on startup: there's nothing useful this service can do without
    Ollama, and a clear early error beats a confusing /generate failure
    later.
    """
    path = shutil.which("ollama")
    if not path:
        msg = (
            "ollama binary not found on PATH. Install it from "
            "https://ollama.com and ensure `ollama serve` is running on "
            f"{config.OLLAMA_URL} before starting llm_service."
        )
        log.error(msg)
        raise OllamaNotInstalled(msg)
    log.info("ollama binary: %s", path)
    return path


async def daemon_reachable() -> bool:
    """True if `${OLLAMA_URL}/api/tags` responds with 200."""
    url = f"{config.OLLAMA_URL}/api/tags"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(url)
        return r.status_code == 200
    except httpx.HTTPError as e:
        log.warning("ollama daemon not reachable at %s: %s", url, e)
        return False


async def model_present(model: str) -> bool:
    """True if `model` appears in `/api/tags`."""
    url = f"{config.OLLAMA_URL}/api/tags"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url)
        if r.status_code != 200:
            return False
        tags = r.json().get("models") or []
        names = {m.get("name") for m in tags if isinstance(m, dict)}
        return model in names
    except (httpx.HTTPError, ValueError):
        return False


async def pull_model(model: str) -> int:
    """Run `ollama pull <model>`. Streams output to our stdout so the user
    sees download progress in the service log. Returns the subprocess
    exit code (0 on success)."""
    log.info("ollama pull %s — this can take several minutes on first run", model)
    proc = await asyncio.create_subprocess_exec(
        "ollama", "pull", model,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    assert proc.stdout is not None
    while True:
        chunk = await proc.stdout.read(4096)
        if not chunk:
            break
        # Write straight through — Ollama emits progress with \r updates
        # which look fine on a terminal and tolerable in the log file.
        sys.stdout.write(chunk.decode(errors="replace"))
        sys.stdout.flush()
    rc = await proc.wait()
    if rc == 0:
        log.info("ollama pull %s: ok", model)
    else:
        log.error("ollama pull %s: exit %d", model, rc)
    return rc


async def ensure_ready() -> None:
    """Full startup pipeline. Raises on unrecoverable failure."""
    ensure_ollama_installed()

    if not await daemon_reachable():
        # Soft-fail: start anyway so /healthz can surface the problem.
        # Operator may be in the middle of `ollama serve`.
        log.warning(
            "ollama daemon not reachable at %s — starting anyway. "
            "/healthz will report ollama=down until the daemon comes up.",
            config.OLLAMA_URL,
        )
        return

    if await model_present(config.OLLAMA_MODEL):
        log.info("model %s already present — skipping pull", config.OLLAMA_MODEL)
        return

    rc = await pull_model(config.OLLAMA_MODEL)
    if rc != 0:
        # Don't crash the service — operator can retry via POST /pull-model
        # or fix Ollama and the next /generate will surface the issue.
        log.error(
            "model pull failed (exit %d). Retry with `curl -X POST "
            "%s/pull-model` once the underlying cause is fixed.",
            rc, config.OLLAMA_URL,
        )
