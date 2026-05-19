"""FastAPI entrypoint for the form-pipeline API.

Endpoints:
- POST /generate-data-json     — async LLM job; returns {job_id, ...}
- GET  /jobs/{job_id}          — current status / progress
- GET  /jobs/{job_id}/data.json — download the produced data.json
- POST /fill-form              — sync; returns the filled PDF/DOCX directly
- GET  /healthz                — llm_service + redis reachability
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError
from scalar_fastapi import get_scalar_api_reference
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from . import auth_utils, config, db as app_db, job_store, llm_service_client, models, usage
from .rate_limit import limiter
from .file_validation import (
    JSON_ONLY,
    MaxBodySizeMiddleware,
    PDF_ONLY,
    PDF_OR_DOCX,
    QUESTIONNAIRE_SUFFIXES,
    REFERENCE_SUFFIXES,
    validate_upload,
)
from .routers import (
    api_keys as api_keys_router,
    auth as auth_router,
    billing as billing_router,
    documents as documents_router,
    review as review_router,
    team as team_router,
    templates as templates_router,
    webhooks as webhooks_router,
)
from .schemas import (
    DataJson,
    HealthResponse,
    JobListItem,
    JobListResponse,
    JobStatus,
    JobStatusResponse,
    JobSubmitResponse,
    ValidateDataJsonResponse,
    ValidationIssue,
)


log = logging.getLogger("api.main")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


# --------------------------------------------------------------------------- #
# App lifespan: arq Redis pool
# --------------------------------------------------------------------------- #

@asynccontextmanager
async def lifespan(app: FastAPI):
    config.JOBS_DIR.mkdir(parents=True, exist_ok=True)
    await app_db.init_models()
    pool: ArqRedis = await create_pool(
        RedisSettings(
            host=config.REDIS_HOST,
            port=config.REDIS_PORT,
            database=config.REDIS_DATABASE,
        )
    )
    app.state.arq = pool
    log.info(
        "api startup: JOBS_DIR=%s REDIS=%s:%d LLM_SERVICE_URL=%s DB=%s",
        config.JOBS_DIR, config.REDIS_HOST, config.REDIS_PORT,
        config.LLM_SERVICE_URL, config.DATABASE_URL,
    )

    # Loud, operator-targeted warnings for footguns that are fine in dev
    # but unacceptable in production. Operator must intentionally flip the
    # env var to silence each one.
    if not config.AUTH_REQUIRED:
        log.warning(
            "AUTH_REQUIRED=0 — every /jobs, /fill-form and /to-acroform "
            "endpoint accepts anonymous callers. Set AUTH_REQUIRED=1 before "
            "exposing this service to the public internet."
        )
    if not config.SESSION_COOKIE_SECURE:
        log.warning(
            "SESSION_COOKIE_SECURE=0 — session cookies will be sent over "
            "plain HTTP. Set SESSION_COOKIE_SECURE=1 when serving via TLS."
        )
    if "*" in config.CORS_ALLOWED_ORIGINS or any(
        o.startswith("http://") and "localhost" not in o and "127.0.0.1" not in o
        for o in config.CORS_ALLOWED_ORIGINS
    ):
        log.warning(
            "CORS_ALLOWED_ORIGINS includes a wildcard or a non-loopback "
            "plain-HTTP origin (%s). Restrict to your dashboard's HTTPS "
            "origin before production deploy.",
            config.CORS_ALLOWED_ORIGINS,
        )
    if not config.WEBHOOK_BLOCK_PRIVATE:
        log.warning(
            "WEBHOOK_BLOCK_PRIVATE=0 — webhook_url SSRF guard is disabled. "
            "Only safe in single-tenant self-hosted deployments."
        )
    if not config.WEBHOOK_SECRET:
        log.warning(
            "WEBHOOK_SECRET is unset — outbound webhooks will be unsigned "
            "and receivers cannot verify the sender. Set WEBHOOK_SECRET to "
            "a long random string in production."
        )

    try:
        yield
    finally:
        await pool.aclose()


app = FastAPI(
    title="form-pipeline data.json generator",
    version="0.2.0",
    description=(
        "Form-pipeline API. Two flows:\n\n"
        "**1. Async — generate `data.json` from a questionnaire + references** "
        "(LLM-backed, 10–60s per call):\n"
        "- `POST /generate-data-json` → returns `{job_id, status_url, download_url}` immediately\n"
        "- `GET /jobs/{job_id}` → `{status, percent, stage, stage_text}`\n"
        "- `GET /jobs/{job_id}/data.json` → result file once `status=\"completed\"`\n\n"
        "**2. Sync — fill a form (PDF or DOCX) with a `data.json`** "
        "(field detection + filling, < 5s):\n"
        "- `POST /fill-form` → streams the filled artifact back inline\n\n"
        "Polished reference at [`/scalar`](/scalar). Swagger UI at "
        "[`/docs`](/docs); ReDoc at [`/redoc`](/redoc)."
    ),
    contact={"name": "form-pipeline", "url": "http://localhost:8000/docs"},
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

# Pin the spec to OpenAPI 3.0 so Swagger UI renders multi-file array fields
# as file pickers (3.1's `contentMediaType` shows as `array<string>` instead
# of an upload widget).
app.openapi_version = "3.0.2"


def _rewrite_binary(node):
    if isinstance(node, dict):
        if (
            node.get("type") == "string"
            and node.get("contentMediaType") == "application/octet-stream"
        ):
            node.pop("contentMediaType", None)
            node["format"] = "binary"
        for v in node.values():
            _rewrite_binary(v)
    elif isinstance(node, list):
        for v in node:
            _rewrite_binary(v)


_original_openapi = app.openapi


def _patched_openapi():
    spec = _original_openapi()
    _rewrite_binary(spec)
    return spec


app.openapi = _patched_openapi

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
    allow_credentials=True,
)

# Reject oversized request bodies BEFORE multipart parsing — defends against
# OOM from a hostile upload. Per-file fine-grained checks still run inside
# each endpoint via validate_upload().
app.add_middleware(MaxBodySizeMiddleware, max_bytes=config.MAX_REQUEST_BYTES)


# Standard browser-protecting headers on every response. CSP is intentionally
# loose for the /docs + /scalar pages (Swagger UI needs inline scripts);
# tighten if you front-end the API with a static dashboard host.
@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    headers = response.headers
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("X-Frame-Options", "DENY")
    headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    headers.setdefault(
        "Permissions-Policy",
        "geolocation=(), microphone=(), camera=(), payment=()",
    )
    # Only emit HSTS on TLS (or when behind a proxy that sets
    # X-Forwarded-Proto=https). Setting it on a plain-HTTP response would
    # pin clients to a port that isn't actually serving HTTPS.
    forwarded = request.headers.get("x-forwarded-proto", "").lower()
    if request.url.scheme == "https" or forwarded == "https":
        headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )
    return response


# --------------------------------------------------------------------------- #
# Rate limiting — per-IP, Redis-backed
# --------------------------------------------------------------------------- #
# `default_limits=[]` means only endpoints explicitly decorated with
# @limiter.limit(...) are throttled; everything else (e.g. /healthz, /jobs
# polling, /docs) is free. Storage is the same Redis the queue uses, so
# multiple uvicorn workers (future) share a counter.

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# --------------------------------------------------------------------------- #
# Sub-routers (auth, api keys; later phases: templates, documents, members,
# webhook deliveries, billing). Tagged so /docs and /scalar group them.
# --------------------------------------------------------------------------- #
app.include_router(auth_router.router)
app.include_router(api_keys_router.router)
app.include_router(templates_router.router)
app.include_router(documents_router.router)
app.include_router(review_router.router)
app.include_router(team_router.router)
app.include_router(webhooks_router.router)
app.include_router(billing_router.router)


# --------------------------------------------------------------------------- #
# Auth helper for legacy endpoints (Phase 2).
# --------------------------------------------------------------------------- #

async def _track_fill(current_user: models.User | None) -> None:
    """Bump the team's fills_count for the current billing period."""
    if current_user is not None:
        await usage.increment(current_user.team_id, fills_count=1)


async def _enforce_fill_quota(current_user: models.User | None) -> None:
    """Reject with 402 if the team is over its monthly fills cap.

    Anonymous callers bypass (legacy / AUTH_REQUIRED=0); flip AUTH_REQUIRED
    to close the gap before going to production.
    """
    if current_user is None:
        return
    from .db import get_sessionmaker
    async with get_sessionmaker()() as session:
        within, used, cap = await usage.check_limit(
            session, current_user.team_id, "fills",
        )
    if not within:
        raise HTTPException(
            status_code=402,
            detail=(
                f"monthly fills quota exceeded ({used}/{cap}). "
                "Upgrade your plan at /billing/checkout."
            ),
        )


def _check_job_access(job_id: str, current_user: models.User | None) -> None:
    """Raise 404 if `current_user` can't access this job.

    - Anonymous callers (current_user is None) can only see jobs created
      without a team_id (legacy / pre-auth state, only reachable when
      AUTH_REQUIRED=0).
    - Authenticated callers can only see jobs in their own team.
    The 404 (vs 403) prevents enumerating other teams' job_ids.
    """
    owns = job_store.team_owns(
        job_id, current_user.team_id if current_user is not None else None,
    )
    if owns is None:
        return  # job doesn't exist — caller's existing state-is-None check 404s
    if not owns:
        raise HTTPException(status_code=404, detail=f"unknown job_id={job_id!r}")


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@app.get(
    "/healthz",
    response_model=HealthResponse,
    tags=["meta"],
    summary="Health check (per-component reachability)",
)
async def healthz() -> HealthResponse:
    llm_health = await llm_service_client.health()
    llm_ok = llm_health.get("ollama") == "ok"
    redis_ok = await _redis_health()
    overall = "ok" if (llm_ok and redis_ok) else "degraded"
    return HealthResponse(
        status=overall,
        llm_service="ok" if llm_ok else "down",
        redis="ok" if redis_ok else "down",
        model=str(llm_health.get("model") or ""),
    )


async def _redis_health() -> bool:
    """True iff a PING round-trip to the arq Redis pool succeeds."""
    pool: ArqRedis | None = getattr(app.state, "arq", None)
    if pool is None:
        return False
    try:
        return bool(await asyncio.wait_for(pool.ping(), timeout=2.0))
    except Exception:
        return False


@app.get("/scalar", include_in_schema=False)
async def scalar_reference():
    """Polished Scalar API reference (alternative to /docs and /redoc)."""
    return get_scalar_api_reference(
        openapi_url=app.openapi_url,
        title=f"{app.title} — API Reference",
    )


@app.post(
    "/generate-data-json",
    response_model=JobSubmitResponse,
    status_code=202,
    tags=["jobs"],
    summary="Submit a generation job (returns immediately)",
    description=(
        "Multipart upload — the call returns as soon as the files are saved "
        "and the job is queued, typically within ~100ms.\n\n"
        "- `questionnaire_file` (required) — the blank form.\n"
        "- `reference_files` (optional, repeatable) — answer-source docs.\n"
        "- `questionnaire_title` (optional) — title override.\n\n"
        "Use the returned `status_url` to poll progress and `download_url` "
        "to fetch the result once `status == \"completed\"`."
    ),
    responses={
        202: {"description": "Job accepted and queued"},
        400: {"description": "Invalid webhook_url"},
        413: {"description": "Upload exceeds size limit"},
        415: {"description": "Unsupported file type"},
    },
)
@limiter.limit(config.RATE_LIMIT_GENERATE)
async def submit_job(
    request: Request,
    response: Response,
    questionnaire_file: UploadFile = File(
        ..., description="Blank questionnaire — PDF / scanned PDF / image / DOCX"
    ),
    reference_files: list[UploadFile] = File(
        default=[],
        description="Zero or more answer-source documents (repeatable field)",
    ),
    questionnaire_title: str | None = Form(
        default=None, description="Optional title override"
    ),
    webhook_url: str | None = Form(
        default=None,
        description=(
            "Optional callback URL (http:// or https://). When set, the API "
            "POSTs the terminal-state payload to this URL on both completion "
            "and failure — replacing forced polling. Body is JSON; if "
            "WEBHOOK_SECRET is configured the request includes "
            "X-Form-Pipeline-Signature: sha256=<hmac> for verification."
        ),
    ),
    current_user: models.User | None = Depends(auth_utils.auth_for_jobs),
) -> JobSubmitResponse:
    validate_upload(
        questionnaire_file,
        allowed_suffixes=QUESTIONNAIRE_SUFFIXES,
        label="questionnaire_file",
    )
    validated_refs: list[UploadFile] = []
    for i, ref in enumerate(reference_files):
        # Skip placeholder uploads (Swagger sometimes posts an empty entry).
        if not ref.filename:
            continue
        validate_upload(
            ref,
            allowed_suffixes=REFERENCE_SUFFIXES,
            label=f"reference_files[{i}]",
        )
        validated_refs.append(ref)

    if webhook_url is not None:
        webhook_url = _validate_webhook_url(webhook_url)

    # Enforce per-team monthly quotas when authenticated. Anonymous callers
    # bypass quotas; flip AUTH_REQUIRED=1 to close the gap.
    if current_user is not None:
        from .db import get_sessionmaker
        async with get_sessionmaker()() as session:
            within, used, cap = await usage.check_limit(
                session, current_user.team_id, "jobs",
            )
        if not within:
            raise HTTPException(
                status_code=402,
                detail=(
                    f"monthly jobs quota exceeded ({used}/{cap}). "
                    "Upgrade your plan at /billing/checkout."
                ),
            )

    job_id = uuid4().hex
    q_name = Path(questionnaire_file.filename or "questionnaire.bin").name

    # Save uploaded files first (so we know the final, deduplicated names),
    # then create the job state with that final list.
    uploads = job_store.uploads_dir(job_id)
    uploads.mkdir(parents=True, exist_ok=True)
    _save_upload(questionnaire_file, uploads / q_name)

    ref_names: list[str] = []
    seen: set[str] = set()
    for upload in validated_refs:
        name = _dedup_name(Path(upload.filename).name, seen | {q_name})
        _save_upload(upload, uploads / name)
        seen.add(name)
        ref_names.append(name)

    job_store.create(
        job_id,
        questionnaire_filename=q_name,
        reference_filenames=ref_names,
        questionnaire_title=questionnaire_title,
        webhook_url=webhook_url,
        team_id=current_user.team_id if current_user is not None else None,
    )

    pool: ArqRedis = app.state.arq
    await pool.enqueue_job("run_generation", job_id)

    log.info(
        "submit_job: job_id=%s q=%r refs=%d",
        job_id, q_name, len(ref_names),
    )
    return JobSubmitResponse(
        job_id=job_id,
        status="queued",
        status_url=f"/jobs/{job_id}",
        download_url=f"/jobs/{job_id}/data.json",
    )


@app.get(
    "/jobs",
    response_model=JobListResponse,
    tags=["jobs"],
    summary="List jobs (paginated, filterable)",
    description=(
        "Returns recent jobs for client recovery and dashboards. Sorted "
        "newest-first by `submitted_at`.\n\n"
        "Filters (all optional):\n"
        "- `status` — repeat for OR semantics, e.g. `?status=completed&status=failed`.\n"
        "- `since` / `until` — ISO 8601 datetimes; `submitted_at >= since` "
        "and `submitted_at < until`.\n"
        "- `limit` — page size (default 50, range 1–200).\n"
        "- `offset` — page start (default 0).\n\n"
        "`total` is the count after filtering, before pagination — paginate "
        "by stepping `offset` until `offset + items.length >= total`."
    ),
    responses={
        200: {"description": "Page of matching jobs"},
        400: {"description": "Malformed since / until datetime"},
        422: {"description": "Bad status value"},
    },
)
async def list_jobs_endpoint(
    status: list[JobStatus] | None = Query(
        default=None,
        description="Filter by one or more statuses (OR'd).",
    ),
    since: str | None = Query(
        default=None,
        description="ISO 8601 datetime; submitted_at >= since.",
    ),
    until: str | None = Query(
        default=None,
        description="ISO 8601 datetime; submitted_at < until.",
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: models.User | None = Depends(auth_utils.auth_for_jobs),
) -> JobListResponse:
    since_dt = _parse_iso_datetime(since, "since") if since else None
    until_dt = _parse_iso_datetime(until, "until") if until else None
    statuses = set(status) if status else None

    if current_user is not None:
        team_filter: int | None | object = current_user.team_id
    else:
        # Anonymous caller (only reachable when AUTH_REQUIRED=0). Restrict to
        # legacy/pre-auth jobs so authed teams' work stays private.
        team_filter = job_store._TEAM_FILTER_ANONYMOUS

    rows = job_store.list_jobs(
        statuses=statuses, since=since_dt, until=until_dt, team_id=team_filter,
    )
    total = len(rows)
    page = rows[offset : offset + limit]
    items = [
        JobListItem(
            **{k: row.get(k) for k in (
                "job_id", "status", "stage", "percent", "submitted_at",
                "started_at", "completed_at", "error",
                "questionnaire_filename", "questionnaire_title", "has_webhook",
            )},
            reference_filenames=row.get("reference_filenames") or [],
            status_url=f"/jobs/{row['job_id']}",
            download_url=(
                f"/jobs/{row['job_id']}/data.json"
                if row.get("status") in _RESULT_AVAILABLE_STATUSES else None
            ),
        )
        for row in page
    ]
    return JobListResponse(total=total, limit=limit, offset=offset, items=items)


_RESULT_AVAILABLE_STATUSES = {"review", "completed"}


@app.get(
    "/jobs/{job_id}",
    response_model=JobStatusResponse,
    tags=["jobs"],
    summary="Job status & progress",
    description=(
        "Returns the current state of a job: status (queued / running / "
        "review / completed / failed), `percent` (0–100), machine-readable "
        "`stage`, and human-readable `stage_text`. Poll this endpoint at ~1 Hz."
    ),
    responses={
        200: {"description": "Current job state"},
        404: {"description": "Unknown job_id"},
    },
)
async def get_job_status(
    job_id: str,
    current_user: models.User | None = Depends(auth_utils.auth_for_jobs),
) -> JobStatusResponse:
    state = job_store.get_state(job_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"unknown job_id={job_id!r}")
    _check_job_access(job_id, current_user)
    download_url = (
        f"/jobs/{job_id}/data.json"
        if state.get("status") in _RESULT_AVAILABLE_STATUSES else None
    )
    return JobStatusResponse(download_url=download_url, **state)


@app.delete(
    "/jobs/{job_id}",
    status_code=204,
    tags=["jobs"],
    summary="Delete a job and all its uploaded files",
    description=(
        "Removes `JOBS_DIR/<job_id>/` recursively, including the original "
        "uploads, the produced `data.json`, and the state file. Works in "
        "any state (queued / running / completed / failed) — useful for "
        "compliance-driven deletion or cancelling a stuck job. If a worker "
        "is mid-run, its pending state writes become no-ops once the dir "
        "is gone (no orphan state.json is recreated)."
    ),
    responses={
        204: {"description": "Deleted"},
        404: {"description": "Unknown job_id"},
    },
)
async def delete_job(
    job_id: str,
    current_user: models.User | None = Depends(auth_utils.auth_for_jobs),
) -> Response:
    _check_job_access(job_id, current_user)
    if not job_store.delete(job_id):
        raise HTTPException(status_code=404, detail=f"unknown job_id={job_id!r}")
    return Response(status_code=204)


@app.get(
    "/jobs/{job_id}/data.json",
    tags=["jobs"],
    summary="Download the produced data.json",
    description=(
        "Returns the result file as `application/json` with "
        "`Content-Disposition: attachment; filename=data.json`. "
        "Returns 409 if the job has not yet completed."
    ),
    responses={
        200: {
            "description": "The data.json file",
            "content": {"application/json": {}},
        },
        404: {"description": "Unknown job_id"},
        409: {"description": "Job not completed yet (or it failed)"},
    },
)
async def download_result(
    job_id: str,
    current_user: models.User | None = Depends(auth_utils.auth_for_jobs),
):
    state = job_store.get_state(job_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"unknown job_id={job_id!r}")
    _check_job_access(job_id, current_user)
    if state.get("status") not in _RESULT_AVAILABLE_STATUSES:
        return JSONResponse(
            status_code=409,
            content={"detail": "job result not available yet", "current": state},
        )
    return FileResponse(
        path=str(job_store.result_path(job_id)),
        media_type="application/json",
        filename="data.json",
    )


@app.get(
    "/jobs/{job_id}/preview",
    tags=["jobs"],
    summary="First-page PNG of the filled output (lazy-cached)",
    description=(
        "Returns a PNG of page 1 of the filled questionnaire so dashboards "
        "and list views can show thumbnails without downloading the whole "
        "PDF.\n\n"
        "On the first call the API runs the fill pipeline (1–6s for typical "
        "PDFs) and caches both `filled.pdf` and `preview-{dpi}.png` in the "
        "job directory; subsequent calls serve the PNG straight from disk "
        "(<50ms). The `X-Preview-Source` response header reports which path "
        "ran (`fresh` | `cache`). DELETE on the job removes the cache too.\n\n"
        "Only PDF questionnaires are supported (DOCX → 415; rendering DOCX "
        "would require a LibreOffice/Word converter we don't ship)."
    ),
    responses={
        200: {
            "description": "Page-1 PNG",
            "content": {"image/png": {}},
        },
        400: {"description": "Fill pipeline failed"},
        404: {"description": "Unknown job_id"},
        409: {"description": "Job not completed yet (or it failed)"},
        415: {"description": "Original questionnaire is DOCX/image, not PDF"},
    },
)
@limiter.limit(config.RATE_LIMIT_FILL_FORM)
async def preview(
    request: Request,
    job_id: str,
    dpi: int = Query(
        default=100, ge=50, le=200,
        description="Render DPI (50–200). Cached PNGs are keyed by this value.",
    ),
    current_user: models.User | None = Depends(auth_utils.auth_for_jobs),
) -> FileResponse:
    state = job_store.get_state(job_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"unknown job_id={job_id!r}")
    _check_job_access(job_id, current_user)
    if state.get("status") not in _RESULT_AVAILABLE_STATUSES:
        return JSONResponse(  # type: ignore[return-value]
            status_code=409,
            content={"detail": "job result not available yet", "current": state},
        )

    preview_png = job_store.job_dir(job_id) / f"preview-{dpi}.png"

    # Fast path: serve the cached PNG.
    if preview_png.exists():
        return FileResponse(
            path=str(preview_png),
            media_type="image/png",
            filename=f"preview-{dpi}.png",
            headers={
                "X-Preview-Source": "cache",
                "Cache-Control": "private, max-age=86400",
            },
        )

    # Slow path: ensure filled.pdf exists, then render page 1.
    filled_pdf = await _ensure_filled_pdf(job_id)

    from pdf2image import convert_from_path

    def _render() -> None:
        images = convert_from_path(
            str(filled_pdf), dpi=dpi, first_page=1, last_page=1,
        )
        if not images:
            raise RuntimeError("pdf2image returned 0 pages")
        # Atomic-ish write so a parallel reader can never see a half-PNG.
        tmp_png = preview_png.with_suffix(preview_png.suffix + ".tmp")
        images[0].save(tmp_png, "PNG", optimize=True)
        tmp_png.replace(preview_png)

    try:
        await asyncio.to_thread(_render)
    except Exception as e:
        log.warning("preview: render failed for job_id=%s: %s", job_id, e)
        raise HTTPException(
            status_code=400,
            detail=f"failed to render preview: {type(e).__name__}: {str(e)[:200]}",
        ) from e

    return FileResponse(
        path=str(preview_png),
        media_type="image/png",
        filename=f"preview-{dpi}.png",
        headers={
            "X-Preview-Source": "fresh",
            "Cache-Control": "private, max-age=86400",
        },
    )


# --------------------------------------------------------------------------- #
# Sync /fill-form — same flow as `make run NAME=<n>`
# --------------------------------------------------------------------------- #

_FILL_FORMAT_OPTIONS = {"flat", "flatlist", "nested"}


@app.post(
    "/jobs/{job_id}/fill",
    tags=["jobs"],
    summary="Fill the original questionnaire with this job's data.json",
    description=(
        "Chained extract+fill in one call: reuses the questionnaire that was "
        "uploaded to `/generate-data-json` and the `data.json` this job "
        "produced, and streams the filled artifact back. Eliminates the "
        "download-data + re-upload-PDF round-trip — the common path becomes "
        "submit → poll → fill.\n\n"
        "All form fields are optional:\n"
        "- `form_file` — PDF or DOCX override (rare; lets you fill a "
        "different template with the extracted data).\n"
        "- `format` — `flat | flatlist | nested` override (default: auto-"
        "detect; the produced data.json is always nested).\n"
        "- `answers_file` — flat `{question_id: answer}` JSON for nested.\n\n"
        "Response headers `X-Fields-Filled`, `X-Fields-Missing`, plus "
        "`X-Form-Source: original | uploaded` so the caller can tell which "
        "form was filled."
    ),
    responses={
        200: {
            "description": "Filled form returned as PDF or DOCX",
            "content": {"application/pdf": {}, "application/octet-stream": {}},
        },
        400: {"description": "Invalid format or pipeline failure"},
        404: {"description": "Unknown job_id"},
        409: {"description": "Job not completed yet (or it failed)"},
        415: {
            "description": (
                "Original questionnaire is not fillable (e.g. image scan) "
                "and no form_file was supplied"
            )
        },
    },
)
@limiter.limit(config.RATE_LIMIT_FILL_FORM)
async def fill_job(
    request: Request,
    job_id: str,
    form_file: UploadFile | None = File(
        default=None,
        description="Optional PDF/DOCX override; defaults to reusing the original questionnaire",
    ),
    answers_file: UploadFile | None = File(
        default=None,
        description="Optional flat {question_id: answer} for nested data",
    ),
    format: str | None = Form(
        default=None,
        description="Optional format override: flat | flatlist | nested",
    ),
    current_user: models.User | None = Depends(auth_utils.auth_for_jobs),
) -> FileResponse:
    if format is not None and format not in _FILL_FORMAT_OPTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"format must be one of {sorted(_FILL_FORMAT_OPTIONS)}, got {format!r}",
        )

    state = job_store.get_state(job_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"unknown job_id={job_id!r}")
    _check_job_access(job_id, current_user)
    if state.get("status") not in _RESULT_AVAILABLE_STATUSES:
        return JSONResponse(  # type: ignore[return-value]
            status_code=409,
            content={"detail": "job result not available yet", "current": state},
        )
    await _enforce_fill_quota(current_user)

    if form_file and form_file.filename:
        validate_upload(form_file, allowed_suffixes=PDF_OR_DOCX, label="form_file")
    if answers_file and answers_file.filename:
        validate_upload(
            answers_file, allowed_suffixes=JSON_ONLY, label="answers_file",
        )

    tmp = tempfile.mkdtemp(prefix="job-fill-")
    tmp_path = Path(tmp)
    try:
        if form_file and form_file.filename:
            form_name = Path(form_file.filename).name
            form_path = tmp_path / form_name
            _save_upload(form_file, form_path)
            form_source = "uploaded"
        else:
            meta = job_store.get_meta(job_id)
            if meta is None:
                raise HTTPException(
                    status_code=404, detail=f"unknown job_id={job_id!r}",
                )
            q_name = meta.get("questionnaire_filename")
            if not q_name:
                raise HTTPException(
                    status_code=415,
                    detail="job has no questionnaire on file; supply form_file",
                )
            if Path(q_name).suffix.lower() not in PDF_OR_DOCX:
                raise HTTPException(
                    status_code=415,
                    detail=(
                        f"original questionnaire {q_name!r} is not fillable "
                        "(only PDF/DOCX can be filled). Upload a form_file "
                        "to fill a different template with this job's data."
                    ),
                )
            src = job_store.uploads_dir(job_id) / q_name
            if not src.exists():
                raise HTTPException(
                    status_code=404,
                    detail=f"questionnaire file missing on disk for job_id={job_id!r}",
                )
            form_path = tmp_path / Path(q_name).name
            shutil.copyfile(src, form_path)
            form_source = "original"

        # Copy this job's result.json into the tmp dir as data.json — the
        # pipeline reads it from a path.
        data_path = tmp_path / "data.json"
        result_src = job_store.result_path(job_id)
        if not result_src.exists():
            raise HTTPException(
                status_code=409,
                detail="job marked completed but result.json is missing",
            )
        shutil.copyfile(result_src, data_path)

        answers_path: Path | None = None
        if answers_file and answers_file.filename:
            answers_path = tmp_path / Path(answers_file.filename).name
            _save_upload(answers_file, answers_path)

        _resp = await _run_fill_and_respond(
            form_path=form_path,
            data_path=data_path,
            answers_path=answers_path,
            format_override=format,
            tmp_dir=tmp,
            extra_headers={"X-Form-Source": form_source},
        )
        await _track_fill(current_user)
        return _resp
    except HTTPException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


@app.post(
    "/fill-form",
    tags=["fill-form"],
    summary="Fill a form (PDF or DOCX) with data.json — synchronous",
    description=(
        "Mirrors `make run NAME=<n>` but as a one-shot HTTP call. Field "
        "detection + filling completes in seconds, so this endpoint runs "
        "synchronously and streams the filled artifact back in the response.\n\n"
        "- `form_file` (required) — PDF or DOCX form.\n"
        "- `data_file` (required) — `data.json` in flat / flat-list / nested "
        "format (auto-detected).\n"
        "- `answers_file` (optional) — flat `{question_id: answer}` JSON used "
        "with the nested format.\n"
        "- `format` (optional) — force `flat`, `flatlist`, or `nested` if "
        "auto-detect picks the wrong one."
    ),
    responses={
        200: {
            "description": "Filled form returned as PDF or DOCX",
            "content": {"application/pdf": {}, "application/octet-stream": {}},
        },
        400: {"description": "Invalid format or no fields detected"},
        415: {"description": "Unsupported file type"},
    },
)
@limiter.limit(config.RATE_LIMIT_FILL_FORM)
async def fill_form(
    request: Request,
    form_file: UploadFile = File(
        ..., description="PDF or DOCX form to fill"
    ),
    data_file: UploadFile = File(
        ..., description="data.json — flat, flat-list, or nested"
    ),
    answers_file: UploadFile | None = File(
        default=None,
        description="Optional flat {question_id: answer} for nested data",
    ),
    format: str | None = Form(
        default=None, description="Optional format override: flat | flatlist | nested"
    ),
    current_user: models.User | None = Depends(auth_utils.auth_for_jobs),
) -> FileResponse:
    if format is not None and format not in _FILL_FORMAT_OPTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"format must be one of {sorted(_FILL_FORMAT_OPTIONS)}, got {format!r}",
        )
    await _enforce_fill_quota(current_user)

    validate_upload(form_file, allowed_suffixes=PDF_OR_DOCX, label="form_file")
    validate_upload(data_file, allowed_suffixes=JSON_ONLY, label="data_file")
    if answers_file and answers_file.filename:
        validate_upload(
            answers_file, allowed_suffixes=JSON_ONLY, label="answers_file",
        )

    form_name = Path(form_file.filename or "form.pdf").name

    # The pipeline writes intermediate files (fields.json, etc.) into a workdir
    # and the final filled artifact alongside. We use a TemporaryDirectory so
    # nothing leaks to disk — the FileResponse copies bytes before exit.
    tmp = tempfile.mkdtemp(prefix="fill-form-")
    tmp_path = Path(tmp)
    try:
        form_path = tmp_path / form_name
        data_path = tmp_path / Path(data_file.filename or "data.json").name
        _save_upload(form_file, form_path)
        _save_upload(data_file, data_path)

        answers_path: Path | None = None
        if answers_file and answers_file.filename:
            answers_path = tmp_path / Path(answers_file.filename).name
            _save_upload(answers_file, answers_path)

        _resp = await _run_fill_and_respond(
            form_path=form_path,
            data_path=data_path,
            answers_path=answers_path,
            format_override=format,
            tmp_dir=tmp,
        )
        await _track_fill(current_user)
        return _resp
    except HTTPException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


# --------------------------------------------------------------------------- #
# Sync /validate-data-json — schema check for flat / flatlist / nested
# --------------------------------------------------------------------------- #

@app.post(
    "/validate-data-json",
    response_model=ValidateDataJsonResponse,
    tags=["validation"],
    summary="Validate a data.json payload before posting it to /fill-form",
    description=(
        "Stateless schema check. Auto-detects whether the upload is in "
        "**flat**, **flat-list**, or **nested** form, then validates against "
        "the corresponding shape. Returns 200 with `{valid, format, errors}` "
        "regardless — invalid is a normal answer to a validation question, "
        "not an HTTP error.\n\n"
        "Use this in CI to catch malformed `data.json` files before paying "
        "for a fill, or after hand-editing an LLM-generated payload."
    ),
    responses={
        200: {"description": "Verdict (valid or invalid; see errors[])"},
        400: {"description": "data_file is not valid JSON"},
        415: {"description": "data_file is not a .json upload"},
    },
)
@limiter.limit(config.RATE_LIMIT_FILL_FORM)
async def validate_data_json(
    request: Request,
    response: Response,
    data_file: UploadFile = File(
        ..., description="data.json — flat, flat-list, or nested",
    ),
    current_user: models.User | None = Depends(auth_utils.auth_for_jobs),
) -> ValidateDataJsonResponse:
    validate_upload(data_file, allowed_suffixes=JSON_ONLY, label="data_file")
    raw = await data_file.read()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise HTTPException(
            status_code=400,
            detail=f"data_file is not valid JSON: {e}",
        )

    from run_pipeline import detect_format
    fmt = detect_format(parsed)

    # Strict path: any dict that carries an `items` key whose entries look
    # like Item objects (carry `question`) is treated as canonical DataJson
    # — we run it through the Pydantic model so missing fields like
    # `questionnaire_title` surface as crisp errors. A bare flatlist
    # (top-level list) skips this path and gets loose validation below.
    if (
        isinstance(parsed, dict)
        and isinstance(parsed.get("items"), list)
        and parsed["items"]
        and isinstance(parsed["items"][0], dict)
        and "question" in parsed["items"][0]
    ):
        try:
            DataJson.model_validate(parsed)
            return ValidateDataJsonResponse(valid=True, format="nested", errors=[])
        except ValidationError as e:
            return ValidateDataJsonResponse(
                valid=False, format="nested",
                errors=_pydantic_errors_to_issues(e),
            )

    if fmt == "flat":
        errors = _validate_flat(parsed)
    elif fmt == "flatlist":
        errors = _validate_flatlist(parsed)
    else:
        # detect_format's "nested" path: deep tree with {id|qid, question}
        # leaves. No Pydantic schema for this rare format — if detect_format
        # accepted it the pipeline will too.
        errors = []
    return ValidateDataJsonResponse(
        valid=not errors, format=fmt, errors=errors,
    )


# --------------------------------------------------------------------------- #
# Sync /to-acroform — convert a PDF to editable AcroForm
# --------------------------------------------------------------------------- #

@app.post(
    "/to-acroform",
    tags=["fill-form"],
    summary="Convert a PDF to an editable AcroForm — synchronous",
    description=(
        "Returns an AcroForm PDF where every detected field is an editable "
        "widget. Reviewers can fix wrong answers inline in any PDF viewer "
        "instead of re-running the pipeline.\n\n"
        "- `form_file` (required) — PDF only.\n"
        "- `data_file` (optional) — `data.json` in flat / flat-list / nested "
        "format. When supplied, widgets are pre-populated from it.\n"
        "- `answers_file` (optional) — flat `{question_id: answer}` JSON for "
        "nested data.\n"
        "- `format` (optional) — `flat | flatlist | nested` override.\n\n"
        "If the input PDF already has an AcroForm, the endpoint takes the "
        "fast path: no detection, no injection. The response includes "
        "`X-Acroform-Source: existing | injected` so the caller can tell "
        "which path ran. Text already drawn on the page (e.g. from a prior "
        "`/fill-form` overlay) is **carried over** into the widget defaults "
        "so an overlay-filled PDF becomes editable again."
    ),
    responses={
        200: {
            "description": "AcroForm PDF",
            "content": {"application/pdf": {}},
        },
        400: {"description": "Invalid format option"},
        415: {"description": "Input is not a PDF"},
    },
)
@limiter.limit(config.RATE_LIMIT_TO_ACROFORM)
async def to_acroform(
    request: Request,
    form_file: UploadFile = File(
        ..., description="PDF to convert to AcroForm"
    ),
    data_file: UploadFile | None = File(
        default=None,
        description="Optional data.json (flat / flatlist / nested)",
    ),
    answers_file: UploadFile | None = File(
        default=None,
        description="Optional flat {question_id: answer} for nested data",
    ),
    format: str | None = Form(
        default=None, description="Optional format override: flat | flatlist | nested"
    ),
    current_user: models.User | None = Depends(auth_utils.auth_for_jobs),
) -> FileResponse:
    if format is not None and format not in _FILL_FORMAT_OPTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"format must be one of {sorted(_FILL_FORMAT_OPTIONS)}, got {format!r}",
        )
    await _enforce_fill_quota(current_user)

    validate_upload(form_file, allowed_suffixes=PDF_ONLY, label="form_file")
    if data_file and data_file.filename:
        validate_upload(
            data_file, allowed_suffixes=JSON_ONLY, label="data_file",
        )
    if answers_file and answers_file.filename:
        validate_upload(
            answers_file, allowed_suffixes=JSON_ONLY, label="answers_file",
        )

    form_name = Path(form_file.filename or "form.pdf").name

    tmp = tempfile.mkdtemp(prefix="to-acroform-")
    tmp_path = Path(tmp)
    try:
        from acroform_writer import (
            count_acroform_fields,
            extract_carry_over_values,
            fill_existing_acroform,
            has_acroform,
            inject_acroform_widgets,
        )
        from starlette.background import BackgroundTask

        cleanup = BackgroundTask(shutil.rmtree, tmp, ignore_errors=True)
        src = tmp_path / form_name
        _save_upload(form_file, src)

        # ---- (1) fast path: input already has AcroForm widgets ------------
        if has_acroform(src):
            if data_file is None or not data_file.filename:
                # No data → return the input unchanged.
                log.info("to_acroform: %s already AcroForm; returning as-is", form_name)
                await _track_fill(current_user)
                return FileResponse(
                    path=str(src),
                    media_type="application/pdf",
                    filename="acroform.pdf",
                    headers={
                        "X-Acroform-Source": "existing",
                        "X-Fields-Total": str(count_acroform_fields(src)),
                        "X-Fields-Filled": "0",
                        "X-Fields-Carried-Over": "0",
                    },
                    background=cleanup,
                )

            # AcroForm + data → still need detect+adapter to map question
            # text in data.json onto widget names; the savings vs the slow
            # path is that we don't inject new widgets.
            from field_detector import detect_fields_to_json as _detect
            from field_normalizer import enrich_json as _normalize_fields

            fields_raw = tmp_path / "fields.json"
            fields_norm = tmp_path / "fields_normalized.json"
            await asyncio.to_thread(_detect, str(src), fields_raw)
            await asyncio.to_thread(_normalize_fields, fields_raw, fields_norm)
            import json as _json
            existing_fields = _json.loads(fields_norm.read_text())["fields"]

            user_data = await asyncio.to_thread(
                _build_filler_data,
                src, data_file, answers_file, format, tmp_path,
                fields=existing_fields,
            )

            out = tmp_path / "acroform.pdf"
            report = await asyncio.to_thread(
                fill_existing_acroform, src, existing_fields, user_data, out,
            )
            log.info(
                "to_acroform: %s already AcroForm; filled %d/%d native widgets",
                form_name, report["num_filled"], report["num_fields"],
            )
            await _track_fill(current_user)
            return FileResponse(
                path=str(out),
                media_type="application/pdf",
                filename="acroform.pdf",
                headers={
                    "X-Acroform-Source": "existing",
                    "X-Fields-Total": str(report["num_fields"]),
                    "X-Fields-Filled": str(report["num_filled"]),
                    "X-Fields-Carried-Over": "0",
                },
                background=cleanup,
            )

        # ---- (3) slow path: detect, optionally fill, inject widgets -------
        from field_detector import detect_fields_to_json
        from field_normalizer import enrich_json

        fields_raw = tmp_path / "fields.json"
        fields_norm = tmp_path / "fields_normalized.json"
        try:
            await asyncio.to_thread(detect_fields_to_json, str(src), fields_raw)
            await asyncio.to_thread(enrich_json, fields_raw, fields_norm)
        except Exception as e:
            log.warning("to_acroform: detection failed for %s: %s", form_name, e)
            raise HTTPException(
                status_code=400,
                detail=(
                    f"could not parse PDF for field detection: {type(e).__name__}: "
                    f"{str(e)[:200]}"
                ),
            ) from e

        import json as _json
        fields = _json.loads(fields_norm.read_text())["fields"]
        if not fields:
            raise HTTPException(
                status_code=400,
                detail=(
                    "no fields detected — confirm the PDF has a text layer "
                    "(not a scan)."
                ),
            )

        user_data: dict | None = None
        if data_file is not None and data_file.filename:
            user_data = await asyncio.to_thread(
                _build_filler_data,
                src, data_file, answers_file, format, tmp_path,
                fields=fields,
            )

        carry_over = await asyncio.to_thread(
            extract_carry_over_values, str(src), fields,
        )

        out = tmp_path / "acroform.pdf"
        report = await asyncio.to_thread(
            inject_acroform_widgets, str(src), fields, user_data, carry_over, out,
        )

        log.info(
            "to_acroform: %s injected %d widgets (filled=%d, carried_over=%d)",
            form_name, report["num_fields"], report["num_filled"],
            report["num_carried_over"],
        )
        await _track_fill(current_user)
        return FileResponse(
            path=str(out),
            media_type="application/pdf",
            filename="acroform.pdf",
            headers={
                "X-Acroform-Source": "injected",
                "X-Fields-Total": str(report["num_fields"]),
                "X-Fields-Filled": str(report["num_filled"]),
                "X-Fields-Carried-Over": str(report["num_carried_over"]),
            },
            background=cleanup,
        )
    except HTTPException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _save_upload(upload: UploadFile, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as f:
        shutil.copyfileobj(upload.file, f)


async def _ensure_filled_pdf(job_id: str) -> Path:
    """Make sure `JOBS_DIR/<job_id>/filled.pdf` exists; create it on demand
    by running the fill pipeline against the original questionnaire and the
    job's `result.json`. Idempotent.

    Concurrency: two callers racing for the same job both run the pipeline
    and one wins the final `replace`. Wasted CPU but correct outcome.
    """
    state = job_store.get_state(job_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"unknown job_id={job_id!r}")
    if state.get("status") not in _RESULT_AVAILABLE_STATUSES:
        raise HTTPException(status_code=409, detail="job result not available yet")

    cached = job_store.job_dir(job_id) / "filled.pdf"
    if cached.exists():
        return cached

    meta = job_store.get_meta(job_id)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"unknown job_id={job_id!r}")
    q_name = meta.get("questionnaire_filename")
    if not q_name or Path(q_name).suffix.lower() != ".pdf":
        raise HTTPException(
            status_code=415,
            detail=(
                f"original questionnaire {q_name!r} is not a PDF — "
                "preview/fill caching is only supported for PDF inputs."
            ),
        )
    src = job_store.uploads_dir(job_id) / q_name
    if not src.exists():
        raise HTTPException(
            status_code=404,
            detail=f"questionnaire missing on disk for job_id={job_id!r}",
        )
    result_src = job_store.result_path(job_id)
    if not result_src.exists():
        raise HTTPException(
            status_code=409,
            detail="job marked completed but result.json is missing",
        )

    tmp = tempfile.mkdtemp(prefix="job-prefill-")
    tmp_path = Path(tmp)
    try:
        form_path = tmp_path / Path(q_name).name
        shutil.copyfile(src, form_path)
        data_path = tmp_path / "data.json"
        shutil.copyfile(result_src, data_path)
        out_path = tmp_path / "filled.pdf"
        workdir = tmp_path / "work"

        from run_pipeline import run as run_form_pipeline
        try:
            await asyncio.to_thread(
                run_form_pipeline,
                str(form_path),
                str(data_path),
                output_pdf=str(out_path),
                workdir=str(workdir),
                format_override=None,
                answers_json=None,
            )
        except Exception as e:
            log.warning(
                "_ensure_filled_pdf: pipeline failed for job_id=%s: %s", job_id, e,
            )
            raise HTTPException(
                status_code=400,
                detail=(
                    f"fill pipeline failed: {type(e).__name__}: {str(e)[:200]}"
                ),
            ) from e
        if not out_path.exists():
            raise HTTPException(
                status_code=400,
                detail="fill pipeline produced no output",
            )
        # `replace` is atomic on POSIX; ensures parallel readers never see a
        # truncated PDF.
        shutil.move(str(out_path), str(cached))
        return cached
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def _run_fill_and_respond(
    *,
    form_path: Path,
    data_path: Path,
    answers_path: Path | None,
    format_override: str | None,
    tmp_dir: str,
    extra_headers: dict[str, str] | None = None,
) -> FileResponse:
    """Run run_pipeline.run() in a thread on already-saved paths, validate
    the output, and stream it back as a FileResponse. Cleans up `tmp_dir`
    after the response body is read. Used by both /fill-form and
    /jobs/{job_id}/fill so they stay in lockstep on error handling and
    headers.
    """
    from starlette.background import BackgroundTask

    suffix = form_path.suffix.lower()
    out_ext = ".docx" if suffix == ".docx" else ".pdf"
    tmp_path = Path(tmp_dir)
    out_path = tmp_path / f"filled{out_ext}"
    workdir = tmp_path / "work"

    # run_pipeline.run() is sync + IO-bound (PDF parsing + Pillow). Push it
    # off the event loop so the API process stays responsive.
    from run_pipeline import run as run_form_pipeline

    try:
        report = await asyncio.to_thread(
            run_form_pipeline,
            str(form_path),
            str(data_path),
            output_pdf=str(out_path),
            workdir=str(workdir),
            format_override=format_override,
            answers_json=str(answers_path) if answers_path else None,
        )
    except Exception as e:
        log.warning("fill pipeline failed for %s: %s", form_path.name, e)
        raise HTTPException(
            status_code=400,
            detail=(
                f"pipeline failed: {type(e).__name__}: {str(e)[:200]}. "
                "Confirm the PDF has a text layer and the data.json is valid."
            ),
        ) from e

    if not out_path.exists() or (
        report.get("num_filled", 0) == 0 and report.get("num_missing", 0) == 0
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "pipeline produced no output — likely no fields detected. "
                "Confirm the PDF has a text layer (not a scan)."
            ),
        )

    log.info(
        "fill pipeline: form=%r filled=%d missing=%d",
        form_path.name,
        report.get("num_filled", 0), report.get("num_missing", 0),
    )

    cleanup = BackgroundTask(shutil.rmtree, tmp_dir, ignore_errors=True)
    media_type = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        if out_ext == ".docx" else "application/pdf"
    )
    headers = {
        "X-Fields-Filled": str(report.get("num_filled", 0)),
        "X-Fields-Missing": str(report.get("num_missing", 0)),
    }
    if extra_headers:
        headers.update(extra_headers)
    return FileResponse(
        path=str(out_path),
        media_type=media_type,
        filename=f"filled{out_ext}",
        headers=headers,
        background=cleanup,
    )


def _pydantic_errors_to_issues(e: ValidationError) -> list[ValidationIssue]:
    """Translate Pydantic's structured errors into our ValidationIssue shape.
    Drops the `input` field that Pydantic includes — too noisy and can leak
    user data into responses."""
    out: list[ValidationIssue] = []
    for err in e.errors():
        out.append(ValidationIssue(
            loc=[str(x) if not isinstance(x, int) else x for x in err.get("loc", ())],
            msg=err.get("msg", ""),
            type=err.get("type", ""),
        ))
    return out


def _validate_flat(parsed: Any) -> list[ValidationIssue]:
    """Flat = dict[str, scalar]. Reject lists, dicts, and other nested
    values. Mirrors the assumption baked into run_pipeline.run() at line ~280
    (the flat branch passes user_data straight through to the filler).
    """
    if not isinstance(parsed, dict):
        return [ValidationIssue(
            loc=[], msg="flat format must be a JSON object", type="type_error",
        )]
    issues: list[ValidationIssue] = []
    for k, v in parsed.items():
        if not isinstance(k, str):
            issues.append(ValidationIssue(
                loc=[k], msg="key must be a string", type="type_error",
            ))
            continue
        if k.startswith("_"):
            # Underscore-prefixed keys are stripped by run_pipeline; allow.
            continue
        if not isinstance(v, (str, int, float, bool)) and v is not None:
            issues.append(ValidationIssue(
                loc=[k],
                msg=f"value must be a scalar (str/int/float/bool/null), got {type(v).__name__}",
                type="type_error",
            ))
    return issues


def _validate_flatlist(parsed: Any) -> list[ValidationIssue]:
    """Flat-list = a list of question/answer dicts. The pipeline accepts
    both the bare top-level list and a dict that wraps it under one of the
    FLATLIST_ITEM_KEYS (e.g. `{"items": [...]}`). Use the same
    `_looks_like_flatlist` probe the pipeline uses so we accept whatever
    the pipeline accepts.
    """
    from run_pipeline import _looks_like_flatlist
    is_fl, items_key, items = _looks_like_flatlist(parsed)
    if not is_fl or items is None:
        return [ValidationIssue(
            loc=[],
            msg=(
                "flatlist format must be a JSON array of question/answer "
                "objects, or a dict whose value at one of "
                "(items, questions, results, data, answers) is such an array"
            ),
            type="type_error",
        )]
    if not items:
        return [ValidationIssue(
            loc=[items_key] if items_key else [],
            msg="flatlist must contain at least one item",
            type="value_error",
        )]
    base_loc: list[str | int] = [items_key] if items_key else []
    q_keys = {"question", "label", "prompt", "title", "text"}
    a_keys = {"extracted_answer", "answer", "value", "response"}
    issues: list[ValidationIssue] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            issues.append(ValidationIssue(
                loc=base_loc + [i],
                msg="item must be a JSON object",
                type="type_error",
            ))
            continue
        if not (set(item.keys()) & q_keys):
            issues.append(ValidationIssue(
                loc=base_loc + [i],
                msg=f"item is missing a question key (one of {sorted(q_keys)})",
                type="missing",
            ))
        if not (set(item.keys()) & a_keys):
            issues.append(ValidationIssue(
                loc=base_loc + [i],
                msg=f"item is missing an answer key (one of {sorted(a_keys)})",
                type="missing",
            ))
    return issues


def _parse_iso_datetime(s: str, field: str) -> datetime:
    """Parse an ISO 8601 datetime; reject anything malformed with HTTP 400.
    Accepts both 'Z' suffix (UTC) and explicit offsets — fromisoformat()
    handles 'Z' from Python 3.11+, which we require."""
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=f"{field} is not a valid ISO 8601 datetime: {s!r} ({e})",
        )


def _validate_webhook_url(url: str) -> str:
    """Reject anything that isn't a syntactically valid http(s) URL or that
    resolves to a private/loopback/link-local/etc. IP (SSRF guard).

    Returns the trimmed URL on success; raises HTTPException(400) otherwise.
    Network reachability of the receiver is NOT checked here — that's what
    the retrying delivery is for.
    """
    import ipaddress
    import socket

    cleaned = url.strip()
    if not cleaned:
        raise HTTPException(
            status_code=400,
            detail="webhook_url must be a non-empty http(s) URL",
        )
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(
            status_code=400,
            detail=(
                "webhook_url must use scheme http or https and include a host "
                f"(got {url!r})"
            ),
        )

    if not config.WEBHOOK_BLOCK_PRIVATE:
        return cleaned

    host = parsed.hostname
    # Resolve every A/AAAA the host points to and reject if ANY of them is
    # unsafe — an attacker can otherwise DNS-rebind to flip a public IP to
    # a private one between validation and delivery (we still call this at
    # request time, so the window is small but real).
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise HTTPException(
            status_code=400,
            detail=f"webhook_url host {host!r} does not resolve: {e}",
        )
    addrs: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        sockaddr = info[4]
        try:
            addrs.append(ipaddress.ip_address(sockaddr[0]))
        except ValueError:
            continue
    if not addrs:
        raise HTTPException(
            status_code=400,
            detail=f"webhook_url host {host!r} resolved to no usable IPs",
        )
    for addr in addrs:
        if (
            addr.is_loopback or addr.is_link_local or addr.is_private
            or addr.is_multicast or addr.is_reserved or addr.is_unspecified
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"webhook_url host {host!r} resolves to a non-public IP "
                    f"({addr}) — set WEBHOOK_BLOCK_PRIVATE=0 if this is "
                    "intentional (self-hosted dev)."
                ),
            )
    return cleaned


def _dedup_name(name: str, taken: set[str]) -> str:
    """If `name` is already in `taken`, append a numeric suffix until unique."""
    if name not in taken:
        return name
    stem, suffix = Path(name).stem, Path(name).suffix
    i = 2
    while f"{stem}-{i}{suffix}" in taken:
        i += 1
    return f"{stem}-{i}{suffix}"


def _build_filler_data(
    source_pdf: Path,
    data_upload: UploadFile,
    answers_upload: UploadFile | None,
    format_override: str | None,
    tmp_dir: Path,
    *,
    fields: list[dict] | None,
) -> dict:
    """Mirror of run_pipeline.run()'s format-detect + adapter-dispatch.
    Returns a {canonical_key: value} dict ready for AcroForm widget injection.

    `fields` is optional — when None we run detection + normalization
    ourselves (used on the AcroForm fast path where the writer doesn't need
    the field list). When supplied we reuse what the caller already produced.
    """
    import json as _json
    from run_pipeline import (
        _looks_like_flatlist,
        detect_format,
        detect_question_answer_keys,
    )

    data_path = tmp_dir / Path(data_upload.filename or "data.json").name
    _save_upload(data_upload, data_path)

    answers_path: Path | None = None
    if answers_upload and answers_upload.filename:
        answers_path = tmp_dir / Path(answers_upload.filename).name
        _save_upload(answers_upload, answers_path)

    user_data_raw = _json.loads(data_path.read_text())
    fmt = format_override or detect_format(user_data_raw)

    if fmt == "flat":
        return {k: v for k, v in user_data_raw.items() if not k.startswith("_")}

    # flatlist / nested both need the (enriched) detected fields.
    if fields is None:
        from field_detector import detect_fields_to_json
        from field_normalizer import enrich_json

        fields_raw = tmp_dir / "fields.json"
        fields_norm = tmp_dir / "fields_normalized.json"
        detect_fields_to_json(str(source_pdf), fields_raw)
        enrich_json(fields_raw, fields_norm)
        fields = _json.loads(fields_norm.read_text())["fields"]
    else:
        fields_norm = tmp_dir / "fields_normalized.json"
        fields_norm.write_text(_json.dumps({"fields": fields}, indent=2))

    fields_enriched = tmp_dir / "fields_enriched.json"

    if fmt == "flatlist":
        from flatlist_adapter import build_flat_user_data, enrich_labels_from_left
        enrich_labels_from_left(str(source_pdf), fields_norm, fields_enriched)
        enriched_fields = _json.loads(fields_enriched.read_text())["fields"]
        is_fl, _, items = _looks_like_flatlist(user_data_raw)
        q_key, cq_key, a_key = detect_question_answer_keys(items)
        flat, _diag = build_flat_user_data(
            enriched_fields, items,
            question_key=q_key, contextualized_key=cq_key, answer_key=a_key,
        )
        return flat

    if fmt == "nested":
        from questionnaire_adapter import (
            build_flat_user_data,
            enrich_labels_with_questions,
        )
        enrich_labels_with_questions(str(source_pdf), fields_norm, fields_enriched)
        enriched_fields = _json.loads(fields_enriched.read_text())["fields"]
        overrides = None
        if answers_path and answers_path.exists():
            overrides = _json.loads(answers_path.read_text())
        flat, _diag = build_flat_user_data(
            enriched_fields, user_data_raw, question_to_answer=overrides,
        )
        return flat

    raise ValueError(f"unknown format: {fmt}")
