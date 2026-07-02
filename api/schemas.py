"""Pydantic models for the form-pipeline API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


Confidence = Literal["HIGH", "MEDIUM", "NONE", "USER"]


class Item(BaseModel):
    contextualized_question: str
    source_file: str = "N/A"
    question: str
    question_number: str = Field(..., description="Sequential F1, F2, ...")
    extracted_answer: str = "-"
    confidence: Confidence = "NONE"


class DataJson(BaseModel):
    questionnaire_title: str
    items: list[Item]


class HealthResponse(BaseModel):
    """Top-level `status` is "ok" iff every dependency is reachable; otherwise
    "degraded" — individual fields say which one is down. We return HTTP 200
    in both cases so monitoring systems can read the body and decide what to
    page on (vs. blocking the load balancer with a 503).

    `llm_service` is "ok" iff the host-native LLM service responds AND its
    upstream Ollama is reachable. `model` mirrors the model name that service
    reports."""
    status: Literal["ok", "degraded"]
    llm_service: Literal["ok", "down"]
    redis: Literal["ok", "down"]
    model: str


# --- async job models ------------------------------------------------------

# "review" is added in Phase 3: a job whose extraction succeeded but where
# at least one item has confidence == "NONE", surfacing for human approval
# before being treated as final. POST /jobs/{id}/approve transitions to
# "completed" (or auto-skips review when every item is HIGH/MEDIUM).
JobStatus = Literal["queued", "running", "review", "completed", "failed"]
JobStage = Literal[
    "queued",
    "extracting_questionnaire",
    "extracting_references",
    "calling_llm_service",
    "saving",
    "review",
    "completed",
    "failed",
]


class GenerateValuesRequest(BaseModel):
    template_id: int = Field(..., ge=1, description="Saved template (field schema) to fill")
    document_ids: list[int] = Field(
        ...,
        min_length=1,
        description="Reference document IDs (from GET /documents?type=reference)",
    )
    questionnaire_title: str | None = Field(
        default=None,
        max_length=255,
        description="Optional title override; defaults to the template name",
    )
    webhook_url: str | None = Field(
        default=None,
        description=(
            "Optional callback URL (http:// or https://). When set, the API "
            "POSTs the terminal-state payload on completion and failure."
        ),
    )


class GenerateTemplateRequest(BaseModel):
    document_id: int = Field(
        ...,
        ge=1,
        description="Template form document ID (from GET /documents?type=template)",
    )
    name: str | None = Field(
        default=None,
        max_length=160,
        description="Template name (defaults from document display name or filename)",
    )


class JobSubmitResponse(BaseModel):
    job_id: str
    status: JobStatus = "queued"
    status_url: str = Field(..., description="GET this URL for progress")
    download_url: str = Field(
        ..., description="GET this URL for the data.json once status is completed"
    )


class JobStatusResponse(BaseModel):
    job_id: str
    status: JobStatus
    percent: int = Field(0, ge=0, le=100)
    stage: JobStage
    stage_text: str
    submitted_at: str
    started_at: str | None = None
    completed_at: str | None = None
    error: str | None = None
    download_url: str | None = None
    questionnaire_filename: str | None = None
    reference_filenames: list[str] = []
    questionnaire_title: str | None = None
    has_webhook: bool = False
    template_id: int | None = None
    document_ids: list[int] = []
    status_url: str | None = None


class WebhookPayload(BaseModel):
    """Shape of the JSON body POSTed to a caller's `webhook_url` when a job
    reaches a terminal state. Documented here so receivers can codegen a
    matching model from the OpenAPI spec; not returned by any endpoint."""
    job_id: str
    status: JobStatus
    stage: JobStage
    submitted_at: str | None = None
    completed_at: str | None = None
    error: str | None = None
    status_url: str
    download_url: str | None = None
    result: DataJson | None = Field(
        default=None,
        description="Inline data.json — populated only when status == 'completed'.",
    )


# --- /validate-data-json ---------------------------------------------------

DataJsonFormat = Literal["flat", "flatlist", "nested"]


class ValidationIssue(BaseModel):
    loc: list[str | int] = Field(
        default_factory=list,
        description="Path to the offending value, e.g. ['items', 0, 'question_number'].",
    )
    msg: str
    type: str


class ValidateDataJsonResponse(BaseModel):
    valid: bool
    format: DataJsonFormat = Field(
        ..., description="Auto-detected payload shape (flat / flatlist / nested)."
    )
    errors: list[ValidationIssue] = []


# --- GET /jobs (list) ------------------------------------------------------

class JobListItem(BaseModel):
    job_id: str
    status: JobStatus
    stage: JobStage
    percent: int = Field(0, ge=0, le=100)
    submitted_at: str
    started_at: str | None = None
    completed_at: str | None = None
    error: str | None = None
    questionnaire_filename: str | None = None
    reference_filenames: list[str] = []
    questionnaire_title: str | None = None
    has_webhook: bool = False
    status_url: str
    download_url: str | None = None


class JobListResponse(BaseModel):
    total: int = Field(..., description="Number of jobs matching the filters (pre-pagination).")
    limit: int
    offset: int
    items: list[JobListItem]
