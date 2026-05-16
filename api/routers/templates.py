"""Template CRUD: list, create, save-from-job, delete."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Path, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import auth_utils, job_store, models
from ..db import get_db


router = APIRouter(tags=["templates"])


class TemplateFieldSchemaItem(BaseModel):
    id: str
    label: str
    type: str = "text"
    required: bool = False


class TemplateCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=160)
    field_schema: list[TemplateFieldSchemaItem] = Field(default_factory=list)


class SaveAsTemplateRequest(BaseModel):
    name: str | None = Field(
        default=None,
        max_length=160,
        description=(
            "Template name. Defaults to the job's questionnaire_title, then "
            "filename, then a generated 'Template from <job_id_prefix>'."
        ),
    )


class TemplateResponse(BaseModel):
    id: int
    name: str
    team_id: int
    source_job_id: str | None
    field_schema: list[TemplateFieldSchemaItem]
    uses: int
    fields: int
    accuracy: float | None
    created_at: datetime
    created_by_user_id: int | None


def _to_response(tpl: models.Template) -> TemplateResponse:
    schema = [TemplateFieldSchemaItem(**item) for item in (tpl.field_schema or [])]
    return TemplateResponse(
        id=tpl.id,
        name=tpl.name,
        team_id=tpl.team_id,
        source_job_id=tpl.source_job_id,
        field_schema=schema,
        uses=tpl.uses,
        fields=len(schema),
        accuracy=tpl.accuracy,
        created_at=tpl.created_at,
        created_by_user_id=tpl.created_by_user_id,
    )


@router.get(
    "/templates",
    response_model=list[TemplateResponse],
    summary="List templates for the current team",
)
async def list_templates(
    user: models.User = Depends(auth_utils.get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[TemplateResponse]:
    result = await db.execute(
        select(models.Template)
        .where(models.Template.team_id == user.team_id)
        .order_by(models.Template.created_at.desc())
    )
    return [_to_response(t) for t in result.scalars()]


@router.post(
    "/templates",
    response_model=TemplateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a blank template",
)
async def create_template(
    body: TemplateCreate,
    user: models.User = Depends(auth_utils.get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TemplateResponse:
    tpl = models.Template(
        team_id=user.team_id,
        created_by_user_id=user.id,
        name=body.name,
        field_schema=[item.model_dump() for item in body.field_schema],
        source_job_id=None,
        uses=0,
        created_at=datetime.now(timezone.utc),
    )
    db.add(tpl)
    await db.commit()
    await db.refresh(tpl)
    return _to_response(tpl)


@router.post(
    "/jobs/{job_id}/save-as-template",
    response_model=TemplateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Save a completed job's field schema as a reusable template",
)
async def save_job_as_template(
    body: SaveAsTemplateRequest = SaveAsTemplateRequest(),
    job_id: str = Path(..., min_length=1, max_length=64),
    user: models.User = Depends(auth_utils.get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TemplateResponse:
    owns = job_store.team_owns(job_id, user.team_id)
    if owns is None or not owns:
        raise HTTPException(status_code=404, detail=f"unknown job_id={job_id!r}")
    state = job_store.get_state(job_id)
    if state is None or state.get("status") not in ("review", "completed"):
        raise HTTPException(
            status_code=409,
            detail="job result not available; only review/completed jobs can be templated",
        )
    rp = job_store.result_path(job_id)
    if not rp.exists():
        raise HTTPException(status_code=409, detail="result.json missing on disk")
    result = json.loads(rp.read_text())
    items = result.get("items") or []
    schema = [
        {
            "id": (it.get("question_number") or "").strip()
                  or f"field_{i+1}",
            "label": (it.get("question") or "").strip(),
            "type": "text",
            "required": (it.get("confidence") != "NONE"),
        }
        for i, it in enumerate(items)
    ]

    meta = job_store.get_meta(job_id) or {}
    tpl_name = body.name or meta.get("questionnaire_title") or meta.get(
        "questionnaire_filename") or f"Template from {job_id[:8]}"

    tpl = models.Template(
        team_id=user.team_id,
        created_by_user_id=user.id,
        name=tpl_name,
        field_schema=schema,
        source_job_id=job_id,
        uses=0,
        created_at=datetime.now(timezone.utc),
    )
    db.add(tpl)
    await db.commit()
    await db.refresh(tpl)
    return _to_response(tpl)


@router.delete(
    "/templates/{template_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a template",
)
async def delete_template(
    template_id: int = Path(..., ge=1),
    user: models.User = Depends(auth_utils.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(models.Template).where(
            models.Template.id == template_id,
            models.Template.team_id == user.team_id,
        )
    )
    tpl = result.scalar_one_or_none()
    if tpl is None:
        raise HTTPException(status_code=404, detail="Template not found")
    await db.delete(tpl)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
