"""Per-field and whole-job review approval for jobs in `review` status.

A field approval rewrites result.json: the item's confidence is bumped from
NONE → MEDIUM (and the value substituted if `value` is supplied), and a
FieldApproval audit row is logged. When the last NONE item is resolved, or
when POST /jobs/{job_id}/approve fires en masse, state.json transitions
review → completed.
"""

from __future__ import annotations

import logging
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from .. import auth_utils, job_store, models
from ..db import get_db
from ..path_params import JobIdPath


log = logging.getLogger("api.review")
router = APIRouter(tags=["review"])


class FieldApproveRequest(BaseModel):
    value: str | None = Field(
        default=None,
        description=(
            "Optional override. If omitted, the field is approved with its "
            "current extracted_answer (or empty string for NONE items)."
        ),
        max_length=4000,
    )
    source_file: str | None = Field(
        default=None,
        description="Optional citation override. Defaults to the existing value.",
        max_length=255,
    )


class JobApproveRequest(BaseModel):
    overrides: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Optional map of question_number → value override. Any field "
            "not present is approved with its current extracted_answer."
        ),
    )


class FieldApprovalResponse(BaseModel):
    job_id: str
    field_number: str
    value: str
    job_status: str


class JobApprovalResponse(BaseModel):
    job_id: str
    approved_field_count: int
    job_status: str


def _check_job(job_id: str, user: models.User) -> dict:
    owns = job_store.team_owns(job_id, user.team_id)
    if owns is None or not owns:
        raise HTTPException(status_code=404, detail=f"unknown job_id={job_id!r}")
    state = job_store.get_state(job_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"unknown job_id={job_id!r}")
    if state.get("status") != "review":
        raise HTTPException(
            status_code=409,
            detail=f"job not in review (status={state.get('status')!r})",
        )
    return state


def _load_result(job_id: str) -> dict:
    rp = job_store.result_path(job_id)
    if not rp.exists():
        raise HTTPException(status_code=409, detail="result.json missing on disk")
    return json.loads(rp.read_text())


async def _maybe_complete(request: Request, job_id: str, result: dict) -> str:
    """If no NONE items remain, transition state.json review → completed and
    enqueue a `job.completed` webhook so consumers learn the job's truly
    done (the initial extraction would have fired `job.review` instead)."""
    none_left = sum(
        1 for it in result.get("items", []) if it.get("confidence") == "NONE"
    )
    if none_left > 0:
        return "review"

    job_store.update_state(
        job_id,
        status="completed",
        stage="completed",
        stage_text="Done",
        completed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )

    # Best-effort webhook delivery — only when a webhook_url was registered
    # on the original job. Failure here must not bubble up; the row already
    # moved to "completed" and the caller can still poll.
    meta = job_store.get_meta(job_id) or {}
    if meta.get("webhook_url"):
        try:
            pool = request.app.state.arq
            await pool.enqueue_job("deliver_webhook", job_id)
        except Exception as e:
            log.warning(
                "_maybe_complete: failed to enqueue webhook for job_id=%s: %s",
                job_id, e,
            )
    return "completed"


@router.post(
    "/jobs/{job_id}/fields/{field_number}/approve",
    response_model=FieldApprovalResponse,
    summary="Approve a single field on a review-state job",
)
async def approve_field(
    body: FieldApproveRequest,
    request: Request,
    job_id: JobIdPath,
    field_number: str = Path(..., min_length=1, max_length=16),
    user: models.User = Depends(auth_utils.get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FieldApprovalResponse:
    _check_job(job_id, user)
    result = _load_result(job_id)

    target = next(
        (it for it in result.get("items", [])
         if it.get("question_number") == field_number),
        None,
    )
    if target is None:
        raise HTTPException(
            status_code=404,
            detail=f"field {field_number!r} not in this job",
        )

    new_value = body.value if body.value is not None else (target.get("extracted_answer") or "")
    if new_value == "-" or new_value == "":
        new_value = ""
    target["extracted_answer"] = new_value or "-"
    if body.source_file is not None:
        target["source_file"] = body.source_file or "N/A"
    if target.get("confidence") == "NONE":
        target["confidence"] = "MEDIUM"
    job_store.write_result(job_id, result)

    db.add(
        models.FieldApproval(
            job_id=job_id,
            field_number=field_number,
            approved_by_user_id=user.id,
            approved_value=new_value,
            approved_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()

    new_status = await _maybe_complete(request, job_id, result)
    return FieldApprovalResponse(
        job_id=job_id,
        field_number=field_number,
        value=new_value,
        job_status=new_status,
    )


@router.post(
    "/jobs/{job_id}/approve",
    response_model=JobApprovalResponse,
    summary="Approve all remaining review items at once",
)
async def approve_job(
    body: JobApproveRequest,
    request: Request,
    job_id: JobIdPath,
    user: models.User = Depends(auth_utils.get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JobApprovalResponse:
    _check_job(job_id, user)
    result = _load_result(job_id)

    approved = 0
    now = datetime.now(timezone.utc)
    for item in result.get("items", []):
        qn = item.get("question_number") or ""
        override = body.overrides.get(qn)
        if override is not None:
            item["extracted_answer"] = override or "-"
        if item.get("confidence") == "NONE":
            if item.get("extracted_answer") in ("", "-"):
                # User left it blank — keep extracted_answer as "-" but mark
                # approved so the job can complete. The audit row preserves
                # who consciously approved an empty value.
                item["extracted_answer"] = "-"
            item["confidence"] = "MEDIUM"
            approved += 1
            db.add(
                models.FieldApproval(
                    job_id=job_id,
                    field_number=qn,
                    approved_by_user_id=user.id,
                    approved_value=item.get("extracted_answer") or "",
                    approved_at=now,
                )
            )
    job_store.write_result(job_id, result)
    await db.commit()

    new_status = await _maybe_complete(request, job_id, result)
    return JobApprovalResponse(
        job_id=job_id,
        approved_field_count=approved,
        job_status=new_status,
    )
