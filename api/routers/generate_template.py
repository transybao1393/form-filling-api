"""POST /v2/generate-template — async template skeleton from a stored form document."""

from __future__ import annotations

import logging
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import auth_utils, models, template_task_store
from ..db import get_db
from ..schemas import GenerateTemplateRequest
from .templates import (
    TemplateGenerationStatusResponse,
    TemplateGenerationSubmitResponse,
    TemplateResponse,
    _to_response,
)


log = logging.getLogger("api.routers.generate_template")

router = APIRouter(tags=["templates"])


@router.post(
    "/v2/generate-template",
    response_model=TemplateGenerationSubmitResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Generate a template skeleton from a stored form document (async)",
    description=(
        "JSON body — same async flow as the legacy multipart endpoint, but "
        "the form must already be uploaded via POST /documents (type=template).\n\n"
        "- `document_id` (required) — template form document ID.\n"
        "- `name` (optional) — template name; defaults from document metadata."
    ),
    responses={
        202: {"description": "Task accepted and queued"},
        400: {"description": "Missing template name"},
        401: {"description": "Authentication required"},
        404: {"description": "Document not found or not a template form"},
        410: {"description": "Document file missing on disk"},
    },
)
async def submit_template_generation_v2(
    request: Request,
    body: GenerateTemplateRequest,
    user: models.User = Depends(auth_utils.get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TemplateGenerationSubmitResponse:
    result = await db.execute(
        select(models.Document).where(
            models.Document.id == body.document_id,
            models.Document.team_id == user.team_id,
            models.Document.type == "template",
        )
    )
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(
            status_code=404,
            detail="Document not found or not a template form",
        )
    if not Path(doc.storage_path).exists():
        raise HTTPException(status_code=410, detail="document file missing on disk")

    tpl_name = (body.name or doc.display_name or doc.filename).strip()
    if not tpl_name:
        raise HTTPException(status_code=400, detail="template name is required")

    task_id = uuid4().hex
    template_task_store.create(
        task_id,
        name=tpl_name,
        team_id=user.team_id,
        user_id=user.id,
        document_id=doc.id,
        document_storage_path=str(doc.storage_path),
    )

    pool = request.app.state.arq
    await pool.enqueue_job("run_template_generation_v2", task_id)

    log.info(
        "submit_template_generation_v2: task_id=%s document_id=%d",
        task_id, doc.id,
    )
    return TemplateGenerationSubmitResponse(
        task_id=task_id,
        status="queued",
        status_url=f"/v2/generate-template/{task_id}",
    )


@router.get(
    "/v2/generate-template/{task_id}",
    response_model=TemplateGenerationStatusResponse,
    summary="Poll v2 template generation status",
)
async def get_template_generation_status_v2(
    task_id: str,
    user: models.User = Depends(auth_utils.get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TemplateGenerationStatusResponse:
    state = template_task_store.get_state(task_id)
    if state is None:
        raise HTTPException(status_code=404, detail="task not found")

    meta = template_task_store.get_meta(task_id)
    if meta is None or meta.get("team_id") != user.team_id:
        raise HTTPException(status_code=404, detail="task not found")

    template: TemplateResponse | None = None
    template_id = state.get("template_id")
    if template_id is not None:
        result = await db.execute(
            select(models.Template).where(
                models.Template.id == template_id,
                models.Template.team_id == user.team_id,
            )
        )
        tpl = result.scalar_one_or_none()
        if tpl is not None:
            template = _to_response(tpl)

    return TemplateGenerationStatusResponse(
        task_id=task_id,
        status=state.get("status", "queued"),
        percent=int(state.get("percent") or 0),
        stage=state.get("stage") or "queued",
        stage_text=state.get("stage_text") or "",
        submitted_at=state.get("submitted_at") or "",
        started_at=state.get("started_at"),
        completed_at=state.get("completed_at"),
        error=state.get("error"),
        template_id=template_id,
        template=template,
    )
