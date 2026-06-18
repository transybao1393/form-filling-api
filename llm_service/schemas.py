"""Pydantic models for the LLM service request/response."""

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


class ReferenceDoc(BaseModel):
    filename: str
    text: str


class GenerateRequest(BaseModel):
    questionnaire_text: str = Field(..., min_length=1)
    references: list[ReferenceDoc] = Field(default_factory=list)
    questionnaire_title: str | None = None


class GenerateResponse(BaseModel):
    data: DataJson


class ExtractFieldsRequest(BaseModel):
    questionnaire_text: str = Field(..., min_length=1)
    questionnaire_title: str | None = None


class ExtractFieldsResponse(BaseModel):
    data: DataJson


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    ollama: Literal["ok", "down"]
    model: str
