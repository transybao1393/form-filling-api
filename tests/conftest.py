"""Shared fixtures for the /to-acroform test suite.

Tests run against the live Docker stack (`docker compose up -d`) on
localhost:8000. They do NOT touch the host venv code path.
"""

from __future__ import annotations

import io
import os
from pathlib import Path

import httpx
import pytest


BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def base_url() -> str:
    return BASE_URL


@pytest.fixture(scope="session")
def http() -> httpx.Client:
    """A single-session httpx client. 60s timeout covers slow-path uploads."""
    with httpx.Client(base_url=BASE_URL, timeout=60.0) as client:
        # Sanity: the API must be up before any test runs.
        try:
            r = client.get("/healthz")
            r.raise_for_status()
        except Exception as e:
            pytest.exit(f"API at {BASE_URL} is not reachable: {e}", returncode=2)
        yield client


@pytest.fixture
def acroform_pdf() -> Path:
    """A PDF that already has native AcroForm widgets (Generali test1)."""
    p = REPO_ROOT / "input" / "test1" / "form.pdf"
    if not p.exists():
        pytest.skip(f"missing fixture: {p}")
    return p


@pytest.fixture
def acroform_data() -> Path:
    return REPO_ROOT / "input" / "test1" / "data.json"


@pytest.fixture
def blank_pdf() -> Path:
    """A non-AcroForm PDF that the detector finds fields in (test6)."""
    p = REPO_ROOT / "input" / "test6" / "questionnaire_blank.pdf"
    if not p.exists():
        pytest.skip(f"missing fixture: {p}")
    return p


@pytest.fixture
def blank_data() -> Path:
    return REPO_ROOT / "input" / "test6" / "data.json"


@pytest.fixture
def tiny_invalid_pdf(tmp_path: Path) -> Path:
    """A 'PDF' with .pdf extension but corrupt content."""
    p = tmp_path / "broken.pdf"
    p.write_bytes(b"%PDF-1.4\nnot actually a real pdf\n%%EOF\n")
    return p


@pytest.fixture
def empty_pdf(tmp_path: Path) -> Path:
    """A valid but empty PDF (a single blank page, no detectable fields)."""
    from pypdf import PdfWriter

    p = tmp_path / "empty.pdf"
    w = PdfWriter()
    w.add_blank_page(width=612, height=792)
    with open(p, "wb") as fh:
        w.write(fh)
    return p


@pytest.fixture
def docx_dummy(tmp_path: Path) -> Path:
    p = tmp_path / "form.docx"
    p.write_bytes(b"PK\x03\x04dummy")    # zip magic + junk
    return p


def upload(name: str, path: Path, mime: str = "application/octet-stream"):
    """Build a multipart entry for httpx files= argument."""
    return (name, (path.name, path.read_bytes(), mime))


def upload_bytes(name: str, filename: str, data: bytes,
                 mime: str = "application/octet-stream"):
    return (name, (filename, data, mime))
