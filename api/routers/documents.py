"""Document CRUD: list, upload (single file), download, delete.

Standalone document store — separate from job uploads. Files land in
JOBS_DIR/_documents/<team_id>/<doc_id>_<safe_name>. Each upload becomes a
row in the `documents` table with content_type, size, optional pages, and
a free-form `tag` string the dashboard uses for filtering.
"""

from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from typing import Literal

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import auth_utils, config, models
from ..db import get_db


router = APIRouter(prefix="/documents", tags=["documents"])

DocumentType = Literal["template", "reference"]
_VALID_TYPES = frozenset({"template", "reference"})


_DOCS_SUBDIR = "_documents"
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_DOC_BYTES = 100 * 1024 * 1024  # 100 MB hard cap on a single doc.


class DocumentResponse(BaseModel):
    id: int
    name: str
    filename: str
    content_type: str
    size_bytes: int
    pages: int | None
    tag: str | None
    type: DocumentType
    created_at: datetime
    uploaded_by_user_id: int | None


class DocumentListResponse(BaseModel):
    total: int
    total_size_bytes: int
    limit: int
    offset: int
    items: list[DocumentResponse]


def _safe_filename(raw: str) -> str:
    base = Path(raw).name
    cleaned = _UNSAFE_FILENAME_CHARS.sub("_", base).strip("._-") or "upload.bin"
    return cleaned[:200]


def _docs_dir(team_id: int) -> Path:
    d = config.JOBS_DIR / _DOCS_SUBDIR / str(team_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _to_response(d: models.Document) -> DocumentResponse:
    return DocumentResponse(
        id=d.id,
        name=d.display_name,
        filename=d.filename,
        content_type=d.content_type,
        size_bytes=d.size_bytes,
        pages=d.pages,
        tag=d.tag,
        type=d.type if d.type in _VALID_TYPES else "reference",
        created_at=d.created_at,
        uploaded_by_user_id=d.uploaded_by_user_id,
    )


def _count_pdf_pages(path: Path) -> int | None:
    try:
        from pypdf import PdfReader
        with path.open("rb") as f:
            return len(PdfReader(f).pages)
    except Exception:
        return None


@router.get(
    "",
    response_model=DocumentListResponse,
    summary="List documents for the current team",
)
async def list_documents(
    type: DocumentType | None = None,
    q: str | None = Query(default=None, max_length=120, description="Search name, filename, or tag"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: models.User = Depends(auth_utils.get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DocumentListResponse:
    filters = [models.Document.team_id == user.team_id]
    if type is not None:
        filters.append(models.Document.type == type)
    if q and q.strip():
        needle = f"%{q.strip().lower()}%"
        filters.append(
            or_(
                func.lower(models.Document.display_name).like(needle),
                func.lower(models.Document.filename).like(needle),
                func.lower(models.Document.tag).like(needle),
            )
        )

    total = (
        await db.execute(
            select(func.count()).select_from(models.Document).where(*filters)
        )
    ).scalar_one()

    total_size_bytes = (
        await db.execute(
            select(func.coalesce(func.sum(models.Document.size_bytes), 0)).where(*filters)
        )
    ).scalar_one()

    result = await db.execute(
        select(models.Document)
        .where(*filters)
        .order_by(models.Document.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    items = [_to_response(d) for d in result.scalars()]
    return DocumentListResponse(
        total=total,
        total_size_bytes=int(total_size_bytes or 0),
        limit=limit,
        offset=offset,
        items=items,
    )


@router.post(
    "",
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a single document",
)
async def upload_document(
    display_name: str | None = Form(default=None, max_length=255),
    tag: str | None = Form(default=None, max_length=64),
    type: str = Form(default="reference", max_length=16),
    file: UploadFile = File(..., description="Document to store"),
    user: models.User = Depends(auth_utils.get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DocumentResponse:
    if not file.filename:
        raise HTTPException(status_code=400, detail="missing filename")
    doc_type = type.strip().lower()
    if doc_type not in _VALID_TYPES:
        raise HTTPException(
            status_code=400,
            detail="type must be 'template' or 'reference'",
        )
    safe = _safe_filename(file.filename)
    now = datetime.now(timezone.utc)

    # Pre-insert the row to get an id, then write the file under that id
    # so we never collide on identical filenames.
    doc = models.Document(
        team_id=user.team_id,
        uploaded_by_user_id=user.id,
        filename=safe,
        display_name=display_name or safe,
        content_type=file.content_type or "application/octet-stream",
        size_bytes=0,
        tag=tag,
        type=doc_type,
        storage_path="",
        created_at=now,
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    dest = _docs_dir(user.team_id) / f"{doc.id}_{safe}"
    written = 0
    try:
        with dest.open("wb") as f:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > _MAX_DOC_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"document exceeds {_MAX_DOC_BYTES // (1024*1024)} MB cap",
                    )
                f.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)
        await db.delete(doc)
        await db.commit()
        raise
    except Exception:
        dest.unlink(missing_ok=True)
        await db.delete(doc)
        await db.commit()
        raise

    pages = _count_pdf_pages(dest) if dest.suffix.lower() == ".pdf" else None

    doc.size_bytes = written
    doc.pages = pages
    doc.storage_path = str(dest)
    await db.commit()
    await db.refresh(doc)
    return _to_response(doc)


@router.get(
    "/{doc_id}/download",
    summary="Download a document by id",
)
async def download_document(
    doc_id: int,
    user: models.User = Depends(auth_utils.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(models.Document).where(
            models.Document.id == doc_id,
            models.Document.team_id == user.team_id,
        )
    )
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")
    p = Path(doc.storage_path)
    if not p.exists():
        raise HTTPException(status_code=410, detail="document file missing on disk")
    return FileResponse(
        path=str(p),
        media_type=doc.content_type,
        filename=doc.filename,
    )


@router.delete(
    "/{doc_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a document",
)
async def delete_document(
    doc_id: int,
    user: models.User = Depends(auth_utils.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(models.Document).where(
            models.Document.id == doc_id,
            models.Document.team_id == user.team_id,
        )
    )
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")
    p = Path(doc.storage_path) if doc.storage_path else None
    await db.delete(doc)
    await db.commit()
    if p is not None:
        p.unlink(missing_ok=True)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
