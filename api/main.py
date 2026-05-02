"""FastAPI entrypoint for the form-pipeline API.

Endpoints:
- POST /generate-data-json     — async LLM job; returns {job_id, ...}
- GET  /jobs/{job_id}          — current status / progress
- GET  /jobs/{job_id}/data.json — download the produced data.json
- POST /fill-form              — sync; returns the filled PDF/DOCX directly
- GET  /healthz                — Ollama reachability
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from scalar_fastapi import get_scalar_api_reference

from . import config, job_store, ollama_client
from .file_validation import (
    JSON_ONLY,
    MaxBodySizeMiddleware,
    PDF_ONLY,
    PDF_OR_DOCX,
    QUESTIONNAIRE_SUFFIXES,
    REFERENCE_SUFFIXES,
    validate_upload,
)
from .schemas import (
    HealthResponse,
    JobStatusResponse,
    JobSubmitResponse,
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
    pool: ArqRedis = await create_pool(
        RedisSettings(
            host=config.REDIS_HOST,
            port=config.REDIS_PORT,
            database=config.REDIS_DATABASE,
        )
    )
    app.state.arq = pool
    log.info(
        "api startup: JOBS_DIR=%s REDIS=%s:%d OLLAMA_URL=%s OLLAMA_MODEL=%s",
        config.JOBS_DIR, config.REDIS_HOST, config.REDIS_PORT,
        config.OLLAMA_URL, config.OLLAMA_MODEL,
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
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Reject oversized request bodies BEFORE multipart parsing — defends against
# OOM from a hostile upload. Per-file fine-grained checks still run inside
# each endpoint via validate_upload().
app.add_middleware(MaxBodySizeMiddleware, max_bytes=config.MAX_REQUEST_BYTES)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@app.get(
    "/healthz",
    response_model=HealthResponse,
    tags=["meta"],
    summary="Health check (Ollama reachability)",
)
async def healthz() -> HealthResponse:
    ok = await ollama_client.health()
    return HealthResponse(ollama="ok" if ok else "down", model=config.OLLAMA_MODEL)


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
        413: {"description": "Upload exceeds size limit"},
        415: {"description": "Unsupported file type"},
    },
)
async def submit_job(
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
    "/jobs/{job_id}",
    response_model=JobStatusResponse,
    tags=["jobs"],
    summary="Job status & progress",
    description=(
        "Returns the current state of a job: status (queued / running / "
        "completed / failed), `percent` (0–100), machine-readable `stage`, "
        "and human-readable `stage_text`. Poll this endpoint at ~1 Hz."
    ),
    responses={
        200: {"description": "Current job state"},
        404: {"description": "Unknown job_id"},
    },
)
async def get_job_status(job_id: str) -> JobStatusResponse:
    state = job_store.get_state(job_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"unknown job_id={job_id!r}")
    download_url = (
        f"/jobs/{job_id}/data.json" if state.get("status") == "completed" else None
    )
    return JobStatusResponse(download_url=download_url, **state)


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
async def download_result(job_id: str):
    state = job_store.get_state(job_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"unknown job_id={job_id!r}")
    if state.get("status") != "completed":
        return JSONResponse(
            status_code=409,
            content={"detail": "job not completed", "current": state},
        )
    return FileResponse(
        path=str(job_store.result_path(job_id)),
        media_type="application/json",
        filename="data.json",
    )


# --------------------------------------------------------------------------- #
# Sync /fill-form — same flow as `make run NAME=<n>`
# --------------------------------------------------------------------------- #

_FILL_FORMAT_OPTIONS = {"flat", "flatlist", "nested"}


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
async def fill_form(
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
) -> FileResponse:
    if format is not None and format not in _FILL_FORMAT_OPTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"format must be one of {sorted(_FILL_FORMAT_OPTIONS)}, got {format!r}",
        )

    validate_upload(form_file, allowed_suffixes=PDF_OR_DOCX, label="form_file")
    validate_upload(data_file, allowed_suffixes=JSON_ONLY, label="data_file")
    if answers_file and answers_file.filename:
        validate_upload(
            answers_file, allowed_suffixes=JSON_ONLY, label="answers_file",
        )

    form_name = Path(form_file.filename or "form.pdf").name
    suffix = Path(form_name).suffix.lower()

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

        out_ext = ".docx" if suffix == ".docx" else ".pdf"
        out_path = tmp_path / f"filled{out_ext}"
        workdir = tmp_path / "work"

        # run_pipeline.run() is sync + IO-bound (PDF parsing + Pillow). Push
        # it off the event loop so the API process stays responsive.
        from run_pipeline import run as run_form_pipeline

        report = await asyncio.to_thread(
            run_form_pipeline,
            str(form_path),
            str(data_path),
            output_pdf=str(out_path),
            workdir=str(workdir),
            format_override=format,
            answers_json=str(answers_path) if answers_path else None,
        )

        if not out_path.exists() or report.get("num_filled", 0) == 0 and report.get("num_missing", 0) == 0:
            raise HTTPException(
                status_code=400,
                detail=(
                    "pipeline produced no output — likely no fields detected. "
                    "Confirm the PDF has a text layer (not a scan)."
                ),
            )

        log.info(
            "fill_form: form=%r data=%r filled=%d missing=%d",
            form_name, data_file.filename,
            report.get("num_filled", 0), report.get("num_missing", 0),
        )

        # FileResponse will read the file as it's streamed, then BackgroundTask
        # cleans up the tempdir.
        from starlette.background import BackgroundTask

        cleanup = BackgroundTask(shutil.rmtree, tmp, ignore_errors=True)
        media_type = (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            if out_ext == ".docx" else "application/pdf"
        )
        download_name = f"filled{out_ext}"
        return FileResponse(
            path=str(out_path),
            media_type=media_type,
            filename=download_name,
            headers={
                "X-Fields-Filled": str(report.get("num_filled", 0)),
                "X-Fields-Missing": str(report.get("num_missing", 0)),
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
async def to_acroform(
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
) -> FileResponse:
    if format is not None and format not in _FILL_FORMAT_OPTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"format must be one of {sorted(_FILL_FORMAT_OPTIONS)}, got {format!r}",
        )

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
