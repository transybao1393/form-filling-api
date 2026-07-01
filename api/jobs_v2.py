"""Worker-side job logic for POST /v2/generate-values.

Reads template_id + document_ids from job meta (via job_store.create_v2),
loads the template field_schema and stored reference documents from the DB,
then reuses the same llm_service / review / webhook flow as v1.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from . import db as app_db, job_store, llm_service_client, models, usage
from .extractors import UnsupportedFileType, extract_text
from .jobs import _maybe_enqueue_webhook
from .schemas import DataJson
from .template_helpers import field_schema_to_questionnaire_text


log = logging.getLogger("api.jobs_v2")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def run_generation_v2(ctx: dict[str, Any], job_id: str) -> None:
    """arq worker for v2 jobs — template field keys + stored reference docs."""
    log.info("run_generation_v2: starting job_id=%s", job_id)
    update = lambda **kw: job_store.update_state(job_id, **kw)

    try:
        meta = job_store.get_meta(job_id)
        if meta is None:
            log.info("run_generation_v2: job_id=%s deleted before start, skipping", job_id)
            return

        template_id = meta.get("template_id")
        document_ids: list[int] = meta.get("document_ids") or []
        if template_id is None or not document_ids:
            raise RuntimeError("v2 job meta missing template_id or document_ids")

        team_id = meta.get("team_id")

        update(
            status="running",
            started_at=_now_iso(),
            percent=10,
            stage="extracting_questionnaire",
            stage_text="Loading template fields",
        )

        sm = app_db.get_sessionmaker()
        async with sm() as session:
            tpl_result = await session.execute(
                select(models.Template).where(models.Template.id == template_id)
            )
            tpl = tpl_result.scalar_one_or_none()
            if tpl is None:
                raise RuntimeError(f"template {template_id} not found")
            if team_id is not None and tpl.team_id != team_id:
                raise RuntimeError(f"template {template_id} not accessible")
            if not tpl.field_schema:
                raise RuntimeError("template has no field_schema")

            q_text = field_schema_to_questionnaire_text(tpl.field_schema)
            if not q_text.strip():
                raise RuntimeError("template field_schema produced empty questionnaire")

            update(
                percent=25,
                stage="extracting_references",
                stage_text="Reading reference documents",
            )

            docs_result = await session.execute(
                select(models.Document).where(
                    models.Document.id.in_(document_ids),
                    models.Document.team_id == tpl.team_id,
                )
            )
            docs_by_id = {d.id: d for d in docs_result.scalars()}
            missing = [i for i in document_ids if i not in docs_by_id]
            if missing:
                raise RuntimeError(f"reference document(s) not found: {missing}")

            refs: list[tuple[str, str]] = []
            for doc_id in document_ids:
                doc = docs_by_id[doc_id]
                path = Path(doc.storage_path)
                if not path.exists():
                    raise RuntimeError(f"document {doc_id} file missing on disk")
                try:
                    text = extract_text(path)
                except UnsupportedFileType as e:
                    raise RuntimeError(
                        f"unsupported reference type for {doc.filename!r}: {e}"
                    ) from e
                if text:
                    refs.append((doc.filename, text))

        update(
            percent=40,
            stage="calling_llm_service",
            stage_text="Generating answers",
        )
        data_dict = await llm_service_client.generate(
            q_text, refs, meta.get("questionnaire_title")
        )
        data = DataJson.model_validate(data_dict)

        update(percent=99, stage="saving", stage_text="Writing data.json")
        job_store.write_result(job_id, data.model_dump())

        needs_review = any(item.confidence == "NONE" for item in data.items)
        terminal_status = "review" if needs_review else "completed"
        terminal_stage = "review" if needs_review else "completed"
        terminal_text = (
            "Awaiting reviewer — open items need a human"
            if needs_review else "Done"
        )

        update(
            percent=100,
            status=terminal_status,
            stage=terminal_stage,
            stage_text=terminal_text,
            completed_at=_now_iso(),
        )
        log.info(
            "run_generation_v2: %s job_id=%s template_id=%s items=%d none=%d",
            terminal_status, job_id, template_id, len(data.items),
            sum(1 for i in data.items if i.confidence == "NONE"),
        )
        await usage.increment(team_id, jobs_count=1)
        await _maybe_enqueue_webhook(ctx, job_id)

    except Exception as e:
        log.exception("run_generation_v2: job_id=%s failed", job_id)
        job_store.mark_failed(job_id, e)
        await _maybe_enqueue_webhook(ctx, job_id)
        raise


async def run_template_generation_v2(ctx: dict[str, Any], task_id: str) -> None:
    """arq worker for POST /v2/generate-template — stored template form document."""
    from . import template_task_store
    from .template_helpers import items_to_field_schema

    log.info("run_template_generation_v2: starting task_id=%s", task_id)
    update = lambda **kw: template_task_store.update_state(task_id, **kw)

    try:
        meta = template_task_store.get_meta(task_id)
        if meta is None:
            log.info("run_template_generation_v2: task_id=%s deleted, skipping", task_id)
            return

        document_id = meta.get("document_id")
        if document_id is None:
            raise RuntimeError("v2 task meta missing document_id")

        team_id = meta["team_id"]

        update(
            status="running",
            started_at=_now_iso(),
            percent=10,
            stage="extracting_form",
            stage_text="Reading the form document",
        )

        form_path: Path | None = None
        if meta.get("document_storage_path"):
            form_path = Path(meta["document_storage_path"])
        else:
            sm = app_db.get_sessionmaker()
            async with sm() as session:
                doc_result = await session.execute(
                    select(models.Document).where(
                        models.Document.id == document_id,
                        models.Document.team_id == team_id,
                        models.Document.type == "template",
                    )
                )
                doc = doc_result.scalar_one_or_none()
                if doc is not None:
                    form_path = Path(doc.storage_path)

        if form_path is None or not form_path.exists():
            raise RuntimeError("form document not found for template generation")

        try:
            q_text = extract_text(form_path)
        except UnsupportedFileType as e:
            raise RuntimeError(f"unsupported form type: {e}") from e
        if not q_text:
            raise RuntimeError(f"could not extract any text from {form_path.name!r}")

        update(
            percent=40,
            stage="calling_llm_service",
            stage_text="Extracting field list",
        )
        data_dict = await llm_service_client.extract_fields(q_text, meta.get("name"))
        schema = items_to_field_schema(data_dict.get("items") or [])
        if not schema:
            raise RuntimeError("LLM returned no fields for this form")

        update(percent=90, stage="saving", stage_text="Saving template")

        sm = app_db.get_sessionmaker()
        async with sm() as session:
            tpl = models.Template(
                team_id=team_id,
                created_by_user_id=meta.get("user_id"),
                name=meta["name"],
                field_schema=schema,
                source_job_id=None,
                source_document_id=document_id,
                uses=0,
                created_at=datetime.now(timezone.utc),
            )
            session.add(tpl)
            await session.commit()
            await session.refresh(tpl)
            template_id = tpl.id

        update(
            percent=100,
            status="completed",
            stage="completed",
            stage_text="Template saved",
            completed_at=_now_iso(),
            template_id=template_id,
        )
        log.info(
            "run_template_generation_v2: completed task_id=%s template_id=%d fields=%d",
            task_id, template_id, len(schema),
        )

    except Exception as e:
        log.exception("run_template_generation_v2: task_id=%s failed", task_id)
        template_task_store.update_state(
            task_id,
            status="failed",
            stage="failed",
            stage_text=f"Failed: {type(e).__name__}",
            completed_at=_now_iso(),
            error=f"{type(e).__name__}: {e}",
        )
        raise
