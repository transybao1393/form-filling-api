"""LLM orchestration service.

Host-native FastAPI process that owns every Ollama-related concern: prompt
building, the /api/chat call, JSON parsing with one-shot repair, and output
normalization. The main app (running in docker-compose) treats this as an
opaque HTTP dependency reached at http://host.docker.internal:11500.

Started by `make ollama-service-up` from the repo root.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse

from . import bootstrap, config, normalize, ollama_client, prompts, util
from .schemas import (
    ExtractFieldsRequest,
    ExtractFieldsResponse,
    GenerateRequest,
    GenerateResponse,
    HealthResponse,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("llm_service")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Verify ollama is installed, daemon reachable, and the configured
    model is pulled before accepting traffic. See bootstrap.ensure_ready."""
    log.info(
        "llm_service startup: OLLAMA_URL=%s OLLAMA_MODEL=%s",
        config.OLLAMA_URL, config.OLLAMA_MODEL,
    )
    await bootstrap.ensure_ready()
    log.info("llm_service ready on :%d", config.LLM_SERVICE_PORT)
    yield


app = FastAPI(
    title="LLM Orchestration Service",
    description=(
        "Host-native companion to the form-pipeline API. Wraps the host's "
        "Ollama daemon with prompt building, structured-JSON parsing, "
        "one-shot repair, and output normalization."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.post("/generate", response_model=GenerateResponse)
async def generate(req: GenerateRequest) -> GenerateResponse:
    """Run the full LLM pipeline: build prompt, call Ollama, parse+repair,
    normalize, and return the validated data.json."""
    refs = [(r.filename, r.text) for r in req.references]
    messages = prompts.build_messages(
        req.questionnaire_text, refs, req.questionnaire_title
    )

    try:
        raw = await ollama_client.chat(messages)
        raw = util.clean_json_output(raw)
    except ollama_client.OllamaError as e:
        log.warning("ollama call failed: %s", e)
        raise HTTPException(status_code=502, detail=f"ollama: {e}") from e

    try:
        data = await normalize.parse_and_validate(raw, messages)
    except Exception as e:
        log.exception("parse_and_validate failed after repair")
        raise HTTPException(
            status_code=502,
            detail=f"model output failed validation: {type(e).__name__}: {e}",
        ) from e

    if req.questionnaire_title:
        data.questionnaire_title = req.questionnaire_title

    known = {r.filename for r in req.references}
    normalize.normalize(data, known)

    # Defensive: renumber to guarantee F1..Fn even if the model drifted.
    for i, item in enumerate(data.items, start=1):
        item.question_number = f"F{i}"

    return GenerateResponse(data=data)


@app.post("/extract-fields", response_model=ExtractFieldsResponse)
async def extract_fields(req: ExtractFieldsRequest) -> ExtractFieldsResponse:
    """Extract field list from a form document (no reference docs)."""
    messages = prompts.build_extract_fields_messages(
        req.questionnaire_text, req.questionnaire_title
    )

    try:
        raw = await ollama_client.chat(messages)
        raw = util.clean_json_output(raw)
    except ollama_client.OllamaError as e:
        log.warning("ollama call failed: %s", e)
        raise HTTPException(status_code=502, detail=f"ollama: {e}") from e

    try:
        data = await normalize.parse_and_validate(raw, messages)
    except Exception as e:
        log.exception("parse_and_validate failed after repair")
        raise HTTPException(
            status_code=502,
            detail=f"model output failed validation: {type(e).__name__}: {e}",
        ) from e

    if req.questionnaire_title:
        data.questionnaire_title = req.questionnaire_title

    normalize.normalize(data, set())

    for i, item in enumerate(data.items, start=1):
        item.question_number = f"F{i}"
        item.extracted_answer = "-"
        item.source_file = "N/A"
        item.confidence = "NONE"

    return ExtractFieldsResponse(data=data)


@app.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    ok = await ollama_client.health()
    return HealthResponse(
        status="ok" if ok else "degraded",
        ollama="ok" if ok else "down",
        model=config.OLLAMA_MODEL,
    )


@app.post("/pull-model")
async def pull_model(model: str | None = None) -> StreamingResponse:
    """Shell out to `ollama pull <model>` and stream its output back so a
    fresh box can be warmed without terminal access."""
    target = model or config.OLLAMA_MODEL

    async def _stream() -> AsyncGenerator[bytes, None]:
        proc = await asyncio.create_subprocess_exec(
            "ollama", "pull", target,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert proc.stdout is not None
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                break
            yield chunk
        rc = await proc.wait()
        yield f"\nollama pull exited with status {rc}\n".encode()

    return StreamingResponse(_stream(), media_type="text/plain")
