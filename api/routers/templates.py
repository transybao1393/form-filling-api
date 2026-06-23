"""Template CRUD, async generation, save-from-job, delete."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path as FsPath
from uuid import uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import auth_utils, job_store, models, template_task_store
from ..db import get_db
from ..file_validation import QUESTIONNAIRE_SUFFIXES, validate_upload
from ..path_params import JobIdPath
from ..template_helpers import items_to_field_schema


router = APIRouter(tags=["templates"])


class TemplateFieldSchemaItem(BaseModel):
    id: str
    label: str
    type: str = "text"
    required: bool = False
    contextualized_question: str | None = None


class TemplateCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=160)
    field_schema: list[TemplateFieldSchemaItem] = Field(default_factory=list)
    document_id: int | None = Field(
        default=None,
        ge=1,
        description="Optional Documents row (kind=template) this template is based on.",
    )


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
    source_document_id: int | None
    field_schema: list[TemplateFieldSchemaItem]
    uses: int
    fields: int
    accuracy: float | None
    created_at: datetime
    created_by_user_id: int | None


class TemplateListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[TemplateResponse]
    pending: list["PendingTemplateItem"] = Field(default_factory=list)


class PendingTemplateItem(BaseModel):
    task_id: str
    name: str
    status: str
    percent: int = 0
    stage_text: str = ""
    submitted_at: str


TemplateListResponse.model_rebuild()


class TemplateGenerationSubmitResponse(BaseModel):
    task_id: str
    status: str = "queued"
    status_url: str


class TemplateGenerationStatusResponse(BaseModel):
    task_id: str
    status: str
    percent: int = 0
    stage: str
    stage_text: str
    submitted_at: str
    started_at: str | None = None
    completed_at: str | None = None
    error: str | None = None
    template_id: int | None = None
    template: TemplateResponse | None = None


def _to_response(tpl: models.Template) -> TemplateResponse:
    schema = [TemplateFieldSchemaItem(**item) for item in (tpl.field_schema or [])]
    return TemplateResponse(
        id=tpl.id,
        name=tpl.name,
        team_id=tpl.team_id,
        source_job_id=tpl.source_job_id,
        source_document_id=tpl.source_document_id,
        field_schema=schema,
        uses=tpl.uses,
        fields=len(schema),
        accuracy=tpl.accuracy,
        created_at=tpl.created_at,
        created_by_user_id=tpl.created_by_user_id,
    )


def _save_upload(upload: UploadFile, dest: FsPath) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as f:
        shutil.copyfileobj(upload.file, f)


@router.get(
    "/templates",
    response_model=TemplateListResponse,
    summary="List templates for the current team",
)
async def list_templates(
    q: str | None = Query(default=None, max_length=120, description="Search template name"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: models.User = Depends(auth_utils.get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TemplateListResponse:
    filters = [models.Template.team_id == user.team_id]
    if q and q.strip():
        needle = f"%{q.strip().lower()}%"
        filters.append(func.lower(models.Template.name).like(needle))
    total = (
        await db.execute(
            select(func.count()).select_from(models.Template).where(*filters)
        )
    ).scalar_one()
    result = await db.execute(
        select(models.Template)
        .where(*filters)
        .order_by(models.Template.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    items = [_to_response(t) for t in result.scalars()]

    in_flight = template_task_store.list_tasks(
        team_id=user.team_id,
        statuses={"queued", "running", "failed"},
    )
    if q and q.strip():
        needle = q.strip().lower()
        in_flight = [
            t for t in in_flight
            if needle in (t.get("name") or "").lower()
        ]
    pending = [
        PendingTemplateItem(
            task_id=t["task_id"],
            name=t.get("name") or "New template",
            status=t.get("status") or "queued",
            percent=int(t.get("percent") or 0),
            stage_text=t.get("stage_text") or "",
            submitted_at=t.get("submitted_at") or "",
        )
        for t in in_flight
    ]

    return TemplateListResponse(
        total=total,
        limit=limit,
        offset=offset,
        items=items,
        pending=pending,
    )


@router.get(
    "/templates/{template_id}",
    response_model=TemplateResponse,
    summary="Get a template by id",
)
async def get_template(
    template_id: int = Path(..., ge=1),
    user: models.User = Depends(auth_utils.get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TemplateResponse:
    result = await db.execute(
        select(models.Template).where(
            models.Template.id == template_id,
            models.Template.team_id == user.team_id,
        )
    )
    tpl = result.scalar_one_or_none()
    if tpl is None:
        raise HTTPException(status_code=404, detail="Template not found")
    return _to_response(tpl)


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
    source_document_id: int | None = None
    if body.document_id is not None:
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
        source_document_id = doc.id

    tpl = models.Template(
        team_id=user.team_id,
        created_by_user_id=user.id,
        name=body.name,
        field_schema=[item.model_dump() for item in body.field_schema],
        source_job_id=None,
        source_document_id=source_document_id,
        uses=0,
        created_at=datetime.now(timezone.utc),
    )
    db.add(tpl)
    await db.commit()
    await db.refresh(tpl)
    return _to_response(tpl)


@router.post(
    "/generate-template",
    response_model=TemplateGenerationSubmitResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Generate a template skeleton from a form (async)",
)
async def submit_template_generation(
    request: Request,
    document_id: int | None = Form(
        default=None,
        description="Documents row (kind=template). XOR with form_file.",
    ),
    # `UploadFile = File(default=None)` (no `| None`) → OpenAPI `string($binary)`
    # with a file picker. `UploadFile | None` becomes anyOf and Swagger shows text.
    form_file: UploadFile = File(
        default=None,
        description="Form upload. XOR with document_id.",
    ),
    name: str | None = Form(
        default=None,
        max_length=160,
        description="Template name (defaults from document display name or filename).",
    ),
    user: models.User = Depends(auth_utils.get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TemplateGenerationSubmitResponse:
    has_doc = document_id is not None
    has_file = form_file is not None and bool(form_file.filename)
    if has_doc == has_file:
        raise HTTPException(
            status_code=400,
            detail="Provide exactly one of document_id or form_file",
        )

    doc_storage_path: str | None = None
    doc_id: int | None = None
    tpl_name: str

    if has_doc:
        assert document_id is not None
        result = await db.execute(
            select(models.Document).where(
                models.Document.id == document_id,
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
        p = FsPath(doc.storage_path)
        if not p.exists():
            raise HTTPException(status_code=410, detail="document file missing on disk")
        doc_storage_path = str(p)
        doc_id = doc.id
        tpl_name = (name or doc.display_name or doc.filename).strip()
    else:
        assert form_file is not None
        validate_upload(
            form_file,
            allowed_suffixes=QUESTIONNAIRE_SUFFIXES,
            label="form_file",
        )
        tpl_name = (name or FsPath(form_file.filename or "form").stem).strip()

    if not tpl_name:
        raise HTTPException(status_code=400, detail="template name is required")

    task_id = uuid4().hex
    form_filename: str | None = None

    if has_file:
        assert form_file is not None
        form_filename = FsPath(form_file.filename or "form.bin").name
        uploads = template_task_store.uploads_dir(task_id)
        uploads.mkdir(parents=True, exist_ok=True)
        _save_upload(form_file, uploads / form_filename)

    template_task_store.create(
        task_id,
        name=tpl_name,
        team_id=user.team_id,
        user_id=user.id,
        document_id=doc_id,
        form_filename=form_filename,
        document_storage_path=doc_storage_path if has_doc else None,
    )

    pool = request.app.state.arq
    await pool.enqueue_job("run_template_generation", task_id)

    return TemplateGenerationSubmitResponse(
        task_id=task_id,
        status="queued",
        status_url=f"/generate-template/{task_id}",
    )


@router.get(
    "/template/{task_id}",
    response_model=TemplateGenerationStatusResponse,
    summary="Poll template generation status",
)
async def get_template_generation_status(
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


@router.post(
    "/jobs/{job_id}/save-as-template",
    response_model=TemplateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Save a completed job's field schema as a reusable template",
)
async def save_job_as_template(
    job_id: JobIdPath,
    body: SaveAsTemplateRequest = SaveAsTemplateRequest(),
    user: models.User = Depends(auth_utils.get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TemplateResponse:
    owns = job_store.team_owns(job_id, user.team_id)
    if owns is None or not owns:
        raise HTTPException(
            status_code=404,
            detail=(
                f"unknown job_id={job_id!r}. job_ids come from "
                "POST /generate-data-json; template integer IDs from "
                "GET /templates are NOT job_ids."
            ),
        )
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
    schema = items_to_field_schema(items)

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
