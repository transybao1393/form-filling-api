"""Pydantic models for the form-pipeline API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


Confidence = Literal["HIGH", "MEDIUM", "NONE"]


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
    ollama: Literal["ok", "down"]
    model: str


# --- async job models ------------------------------------------------------

JobStatus = Literal["queued", "running", "completed", "failed"]
JobStage = Literal[
    "queued",
    "extracting_questionnaire",
    "extracting_references",
    "building_prompt",
    "calling_llm",
    "normalizing",
    "saving",
    "completed",
    "failed",
]


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
