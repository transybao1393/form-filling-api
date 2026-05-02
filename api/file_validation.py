"""Shared file-upload validation for every API endpoint that takes files.

Two layers of defense:

1. **HTTP-level body cap** — `MaxBodySizeMiddleware` reads `Content-Length`
   and rejects oversize requests with 413 *before* multipart parsing runs.
   Avoids OOM from a hostile or buggy client uploading multi-GB blobs.

2. **Per-file validation** — `validate_upload(...)` checks each upload's
   filename presence, extension allowlist, and decoded byte size after
   multipart parsing. Endpoints call it once per file param, supplying the
   appropriate `allowed_suffixes` and `max_size_mb` for that role.

Standard error responses (machine-friendly JSON):

  | Code | When                                          |
  |------|-----------------------------------------------|
  | 413  | request body / per-file size exceeds limit    |
  | 415  | unsupported file extension or empty filename  |
  | 422  | (FastAPI default) required upload missing     |
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import HTTPException, UploadFile
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import config


# --------------------------------------------------------------------------- #
# Per-role suffix allowlists. Centralised so endpoints share one source of
# truth and tests can reuse the exact same sets the endpoints enforce.
# --------------------------------------------------------------------------- #

PDF_ONLY: frozenset[str] = frozenset({".pdf"})
PDF_OR_DOCX: frozenset[str] = frozenset({".pdf", ".docx"})
JSON_ONLY: frozenset[str] = frozenset({".json"})

# Inputs to /generate-data-json — questionnaire role (form to extract questions from).
QUESTIONNAIRE_SUFFIXES: frozenset[str] = frozenset({
    ".pdf", ".docx",
    ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp",
})

# Inputs to /generate-data-json — reference docs (answer sources).
REFERENCE_SUFFIXES: frozenset[str] = frozenset({
    ".pdf", ".docx", ".xlsx", ".xls", ".pptx",
    ".md", ".txt",
    ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp",
})


# --------------------------------------------------------------------------- #
# Per-file validator
# --------------------------------------------------------------------------- #

def _peek_size(upload: UploadFile) -> int:
    """Return the decoded byte size of an UploadFile without consuming it."""
    f = upload.file
    f.seek(0, os.SEEK_END)
    size = f.tell()
    f.seek(0)
    return size


def validate_upload(
    upload: UploadFile,
    *,
    allowed_suffixes: frozenset[str] | set[str],
    max_size_mb: int | None = None,
    label: str = "file",
) -> UploadFile:
    """Validate a single upload — filename present, extension allowed, size in range.

    Raises HTTPException with the appropriate status code and a JSON detail.
    Returns the upload unchanged so callers can chain.
    """
    name = upload.filename or ""
    if not name.strip():
        raise HTTPException(
            status_code=415,
            detail=f"{label}: no filename supplied",
        )

    suffix = Path(name).suffix.lower()
    if not suffix:
        raise HTTPException(
            status_code=415,
            detail=(
                f"{label}: no file extension on {name!r}. "
                f"Supported: {sorted(allowed_suffixes)}"
            ),
        )
    if suffix not in allowed_suffixes:
        raise HTTPException(
            status_code=415,
            detail=(
                f"{label}: extension {suffix!r} is not supported. "
                f"Supported: {sorted(allowed_suffixes)}"
            ),
        )

    cap_mb = max_size_mb if max_size_mb is not None else config.MAX_UPLOAD_MB
    cap_bytes = cap_mb * 1024 * 1024
    size = _peek_size(upload)
    if size == 0:
        raise HTTPException(
            status_code=415,
            detail=f"{label}: empty file",
        )
    if size > cap_bytes:
        raise HTTPException(
            status_code=413,
            detail=(
                f"{label}: {size / (1024 * 1024):.1f} MB exceeds the "
                f"{cap_mb} MB limit"
            ),
        )

    return upload


# --------------------------------------------------------------------------- #
# HTTP-level body cap
# --------------------------------------------------------------------------- #

class MaxBodySizeMiddleware(BaseHTTPMiddleware):
    """Reject requests whose `Content-Length` exceeds `max_bytes`.

    Runs before multipart parsing, so a hostile 1 GB upload is rejected at
    the HTTP layer rather than buffered into memory. Per-file validation
    (`validate_upload`) still applies after multipart parsing for finer-
    grained constraints.
    """

    def __init__(self, app, max_bytes: int):
        super().__init__(app)
        self.max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next):
        cl = request.headers.get("content-length")
        if cl is not None:
            try:
                if int(cl) > self.max_bytes:
                    return JSONResponse(
                        status_code=413,
                        content={
                            "detail": (
                                f"request body of {int(cl) / (1024*1024):.1f} MB "
                                f"exceeds the {self.max_bytes // (1024*1024)} MB "
                                f"server limit"
                            )
                        },
                    )
            except ValueError:
                # Malformed Content-Length — let the server handle it.
                pass
        return await call_next(request)
