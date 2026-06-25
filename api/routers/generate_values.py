"""POST /v2/generate-values — template_id + document_ids job submission."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import auth_utils, config, job_store, models, usage
from ..db import get_db
from ..rate_limit import limiter
from ..schemas import GenerateValuesRequest, JobSubmitResponse
from ..template_helpers import field_schema_to_questionnaire_text


log = logging.getLogger("api.routers.generate_values")

router = APIRouter(tags=["jobs"])


def _dedup_name(name: str, taken: set[str]) -> str:
    if name not in taken:
        return name
    stem, suffix = Path(name).stem, Path(name).suffix
    i = 2
    while f"{stem}-{i}{suffix}" in taken:
        i += 1
    return f"{stem}-{i}{suffix}"


def _validate_webhook_url(url: str) -> str:
    """Minimal webhook URL check for v2 submit (mirrors main._validate_webhook_url)."""
    from urllib.parse import urlparse

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
    return cleaned


async def _stage_uploads(
    *,
    job_id: str,
    tpl: models.Template,
    docs_by_id: dict[int, models.Document],
    document_ids: list[int],
    db: AsyncSession,
) -> tuple[str, list[str]]:
    """Copy reference docs (and optional template source PDF) into job uploads."""
    uploads = job_store.uploads_dir(job_id)
    uploads.mkdir(parents=True, exist_ok=True)

    q_name = "questionnaire.txt"
    q_text = field_schema_to_questionnaire_text(tpl.field_schema)
    (uploads / q_name).write_text(q_text, encoding="utf-8")

    taken: set[str] = {q_name}
    if tpl.source_document_id is not None:
        src = docs_by_id.get(tpl.source_document_id)
        if src is None:
            src_result = await db.execute(
                select(models.Document).where(
                    models.Document.id == tpl.source_document_id,
                    models.Document.team_id == tpl.team_id,
                )
            )
            src = src_result.scalar_one_or_none()
        if src is not None:
            src_path = Path(src.storage_path)
            if src_path.exists() and src_path.suffix.lower() == ".pdf":
                pdf_name = _dedup_name(src.filename, taken)
                shutil.copy2(src_path, uploads / pdf_name)
                taken.add(pdf_name)
                q_name = pdf_name

    ref_names: list[str] = []
    for doc_id in document_ids:
        doc = docs_by_id[doc_id]
        name = _dedup_name(doc.filename, taken)
        shutil.copy2(doc.storage_path, uploads / name)
        taken.add(name)
        ref_names.append(name)

    return q_name, ref_names


@router.post(
    "/v2/generate-values",
    response_model=JobSubmitResponse,
    status_code=202,
    summary="Submit a generation job from template + stored documents",
    description=(
        "JSON body — same async job flow as POST /generate-data-json, but "
        "inputs are a saved template and reference documents from "
        "POST /documents.\n\n"
        "- `template_id` — template with exact field keys in `field_schema`.\n"
        "- `document_ids` — one or more reference document IDs.\n"
        "- `questionnaire_title` (optional) — title override.\n"
        "- `webhook_url` (optional) — terminal-state callback.\n\n"
        "Poll `status_url`; fetch `download_url` when `status` is "
        "`completed` or `review`."
    ),
    responses={
        202: {"description": "Job accepted and queued"},
        400: {"description": "Invalid webhook_url or empty template schema"},
        401: {"description": "Authentication required"},
        402: {"description": "Monthly jobs quota exceeded"},
        404: {"description": "Template or document not found"},
        410: {"description": "Document file missing on disk"},
    },
)
@limiter.limit(config.RATE_LIMIT_GENERATE)
async def submit_generate_values(
    request: Request,
    response: Response,
    body: GenerateValuesRequest,
    current_user: models.User = Depends(auth_utils.get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JobSubmitResponse:
    webhook_url = (
        _validate_webhook_url(body.webhook_url) if body.webhook_url else None
    )

    tpl_result = await db.execute(
        select(models.Template).where(
            models.Template.id == body.template_id,
            models.Template.team_id == current_user.team_id,
        )
    )
    tpl = tpl_result.scalar_one_or_none()
    if tpl is None:
        raise HTTPException(status_code=404, detail="template not found")
    if not tpl.field_schema:
        raise HTTPException(
            status_code=400,
            detail="template has no fields; generate or create a template first",
        )

    unique_doc_ids = list(dict.fromkeys(body.document_ids))
    docs_result = await db.execute(
        select(models.Document).where(
            models.Document.id.in_(unique_doc_ids),
            models.Document.team_id == current_user.team_id,
        )
    )
    docs_by_id = {d.id: d for d in docs_result.scalars()}
    missing = [i for i in unique_doc_ids if i not in docs_by_id]
    if missing:
        raise HTTPException(
            status_code=404,
            detail=f"document(s) not found: {missing}",
        )
    for doc_id in unique_doc_ids:
        doc = docs_by_id[doc_id]
        if doc.type != "reference":
            raise HTTPException(
                status_code=400,
                detail=f"document {doc_id} is not a reference document",
            )
        if not Path(doc.storage_path).exists():
            raise HTTPException(
                status_code=410,
                detail=f"document {doc_id} file missing on disk",
            )

    within, used, cap = await usage.check_limit(db, current_user.team_id, "jobs")
    if not within:
        raise HTTPException(
            status_code=402,
            detail=(
                f"monthly jobs quota exceeded ({used}/{cap}). "
                "Upgrade your plan at /billing/checkout."
            ),
        )

    job_id = uuid4().hex
    questionnaire_title = body.questionnaire_title or tpl.name
    q_name, ref_names = await _stage_uploads(
        job_id=job_id,
        tpl=tpl,
        docs_by_id=docs_by_id,
        document_ids=unique_doc_ids,
        db=db,
    )

    tpl.uses += 1
    await db.commit()

    job_store.create_v2(
        job_id,
        template_id=tpl.id,
        document_ids=unique_doc_ids,
        questionnaire_filename=q_name,
        reference_filenames=ref_names,
        questionnaire_title=questionnaire_title,
        webhook_url=webhook_url,
        team_id=current_user.team_id,
    )

    pool = request.app.state.arq
    await pool.enqueue_job("run_generation_v2", job_id)

    log.info(
        "submit_generate_values: job_id=%s template_id=%d refs=%d",
        job_id, tpl.id, len(ref_names),
    )
    return JobSubmitResponse(
        job_id=job_id,
        status="queued",
        status_url=f"/jobs/{job_id}",
        download_url=f"/jobs/{job_id}/data.json",
    )
