"""Shared fixtures for the API test suite.

Tests run against the live Docker stack (`docker compose up -d`) on
localhost:8000. They do NOT touch the host venv code path.

Environment variables:
    API_BASE_URL   default http://localhost:8000
    LLM_TESTS      "1" to enable tests that require Ollama (slow, 10-60s
                   each); default off so CI / smoke runs stay fast.
    LLM_TIMEOUT    seconds to wait for an LLM job to complete (default 180)
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from tests._helpers import BASE_URL, LLM_TESTS_ENABLED, REPO_ROOT


# --------------------------------------------------------------------------- #
# Pytest config — register custom marker so `-m llm` works without warnings
# --------------------------------------------------------------------------- #

def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "llm: test requires Ollama; gated by LLM_TESTS=1",
    )


def pytest_collection_modifyitems(config, items):
    if LLM_TESTS_ENABLED:
        return
    skip = pytest.mark.skip(reason="LLM_TESTS=0; set LLM_TESTS=1 to enable")
    for item in items:
        if "llm" in item.keywords:
            item.add_marker(skip)


# --------------------------------------------------------------------------- #
# HTTP client / base URL
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="session")
def base_url() -> str:
    return BASE_URL


@pytest.fixture(scope="session")
def http() -> httpx.Client:
    """A single-session httpx client. 60s timeout covers slow-path uploads."""
    with httpx.Client(base_url=BASE_URL, timeout=60.0) as client:
        try:
            r = client.get("/healthz")
            r.raise_for_status()
        except Exception as e:
            pytest.exit(f"API at {BASE_URL} is not reachable: {e}", returncode=2)
        yield client


# --------------------------------------------------------------------------- #
# PDF / data fixtures (real input/ files)
# --------------------------------------------------------------------------- #

def _first_existing(*candidates: Path) -> Path | None:
    return next((p for p in candidates if p.exists()), None)


@pytest.fixture
def blank_pdf() -> Path:
    """A non-AcroForm PDF that the detector finds fields in."""
    p = _first_existing(
        REPO_ROOT / "input" / "test6" / "questionnaire_blank.pdf",
        REPO_ROOT / "input" / "test3" / "blank_questionnaire.pdf",
    )
    if p is None:
        pytest.skip("no blank-PDF fixture under input/")
    return p


@pytest.fixture
def blank_data() -> Path:
    p = _first_existing(
        REPO_ROOT / "input" / "test6" / "data.json",
        REPO_ROOT / "input" / "test3" / "data.json",
    )
    if p is None:
        pytest.skip("no data.json fixture under input/")
    return p


@pytest.fixture(scope="session")
def acroform_pdf(tmp_path_factory) -> Path:
    """A PDF with native AcroForm widgets.

    Bootstrap: hit POST /to-acroform on a blank PDF (no data) and cache the
    resulting AcroForm PDF for re-use across the session. Self-contained —
    avoids a checked-in binary fixture.
    """
    src = _first_existing(
        REPO_ROOT / "input" / "test1" / "form.pdf",
        REPO_ROOT / "input" / "test6" / "questionnaire_blank.pdf",
        REPO_ROOT / "input" / "test3" / "blank_questionnaire.pdf",
    )
    if src is None:
        pytest.skip("no PDF fixture available to bootstrap acroform_pdf")

    from pypdf import PdfReader
    try:
        reader = PdfReader(str(src))
        if reader.trailer["/Root"].get("/AcroForm") is not None:
            return src
    except Exception:
        pass

    cache = tmp_path_factory.mktemp("acroform-fixture") / "acroform.pdf"
    with httpx.Client(base_url=BASE_URL, timeout=120.0) as c:
        r = c.post(
            "/to-acroform",
            files=[("form_file", (src.name, src.read_bytes(), "application/pdf"))],
        )
    if r.status_code != 200:
        pytest.skip(f"could not bootstrap acroform fixture: {r.status_code} {r.text[:200]}")
    cache.write_bytes(r.content)
    return cache


@pytest.fixture
def acroform_data() -> Path:
    """data.json compatible with the acroform_pdf fixture."""
    p = _first_existing(
        REPO_ROOT / "input" / "test1" / "data.json",
        REPO_ROOT / "input" / "test6" / "data.json",
        REPO_ROOT / "input" / "test3" / "data.json",
    )
    if p is None:
        pytest.skip("no data.json fixture under input/")
    return p


@pytest.fixture
def reference_doc(tmp_path: Path) -> Path:
    """A small text reference doc usable as `reference_files` input."""
    p = tmp_path / "reference.txt"
    p.write_text(
        "Acme Corp ESG report 2024.\n"
        "Headquarters: Hong Kong.\n"
        "Total emissions: 12,345 tonnes CO2e.\n"
        "Female board members: 3 of 8.\n"
    )
    return p


# --------------------------------------------------------------------------- #
# Synthetic / corrupt fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def tiny_invalid_pdf(tmp_path: Path) -> Path:
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
    p.write_bytes(b"PK\x03\x04dummy")
    return p


@pytest.fixture
def malformed_json(tmp_path: Path) -> Path:
    p = tmp_path / "bad.json"
    p.write_text("{ this is not json")
    return p
