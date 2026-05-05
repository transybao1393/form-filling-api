"""Per-IP rate-limit tests.

The slowapi middleware is configured per-endpoint:

    /generate-data-json   → RATE_LIMIT_GENERATE     default 10/minute
    /fill-form            → RATE_LIMIT_FILL_FORM    default 30/minute
    /to-acroform          → RATE_LIMIT_TO_ACROFORM  default 30/minute

To keep the tests fast and deterministic the Docker stack must run with
tight overrides set as env vars on the api container, e.g.:

    -e RATE_LIMIT_GENERATE=3/minute
    -e RATE_LIMIT_FILL_FORM=3/minute
    -e RATE_LIMIT_TO_ACROFORM=3/minute

The Makefile docker-test target injects these so tests run in seconds.
When the override isn't set (running against a real prod stack), the
tests skip gracefully.

slowapi behaviour:
    * 429 Too Many Requests once the bucket is exhausted
    * Retry-After header on the 429 response
    * Counter is per-(IP, endpoint), so /fill-form quota is independent
      from /to-acroform quota
"""

from __future__ import annotations

import os

import httpx
import pytest


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _tight_limit_in_effect(env_var: str, max_calls: int = 5) -> bool:
    """True if the configured limit is tight enough to exercise within a test.

    We expect "<n>/<period>" with n <= max_calls. If absent or wider, skip.
    """
    raw = os.getenv(env_var, "")
    if "/" not in raw:
        return False
    n_str, _, _ = raw.partition("/")
    try:
        return 1 <= int(n_str.strip()) <= max_calls
    except ValueError:
        return False


def _files(form_path, data_path=None):
    f = [("form_file", (form_path.name, form_path.read_bytes(), "application/pdf"))]
    if data_path:
        f.append(("data_file",
                  (data_path.name, data_path.read_bytes(), "application/json")))
    return f


# --------------------------------------------------------------------------- #
# /to-acroform — a fast endpoint, ideal for hammering
# --------------------------------------------------------------------------- #

class TestToAcroformRateLimit:

    @pytest.fixture(autouse=True)
    def _check_env(self):
        if not _tight_limit_in_effect("RATE_LIMIT_TO_ACROFORM"):
            pytest.skip(
                "RATE_LIMIT_TO_ACROFORM not set tight (e.g. 3/minute) — "
                "skipping; run `make docker-test` which configures it"
            )

    def test_429_after_quota_exhausted(self, http, blank_pdf):
        """Hit the endpoint past the per-minute quota and expect a 429."""
        n = int(os.environ["RATE_LIMIT_TO_ACROFORM"].split("/")[0])
        # Exhaust the bucket.
        for i in range(n):
            r = http.post("/to-acroform", files=_files(blank_pdf))
            assert r.status_code in (200, 400, 415), \
                f"request {i} unexpectedly failed: {r.status_code} {r.text[:100]}"
        # Next call must be throttled.
        r = http.post("/to-acroform", files=_files(blank_pdf))
        assert r.status_code == 429, \
            f"expected 429 after {n} calls, got {r.status_code}"

    def test_429_includes_retry_after(self, http, blank_pdf):
        n = int(os.environ["RATE_LIMIT_TO_ACROFORM"].split("/")[0])
        for _ in range(n):
            http.post("/to-acroform", files=_files(blank_pdf))
        r = http.post("/to-acroform", files=_files(blank_pdf))
        assert r.status_code == 429
        # slowapi sets Retry-After on the 429.
        assert "retry-after" in {k.lower() for k in r.headers.keys()}, \
            f"missing Retry-After on 429: {dict(r.headers)}"


# --------------------------------------------------------------------------- #
# /fill-form — independent counter from /to-acroform
# --------------------------------------------------------------------------- #

class TestFillFormRateLimit:

    @pytest.fixture(autouse=True)
    def _check_env(self):
        if not _tight_limit_in_effect("RATE_LIMIT_FILL_FORM"):
            pytest.skip("RATE_LIMIT_FILL_FORM not set tight; skipping")

    def test_429_after_quota_exhausted(self, http, blank_pdf, blank_data):
        n = int(os.environ["RATE_LIMIT_FILL_FORM"].split("/")[0])
        for i in range(n):
            r = http.post("/fill-form", files=_files(blank_pdf, blank_data))
            assert r.status_code in (200, 400, 415), \
                f"request {i} failed: {r.status_code}"
        r = http.post("/fill-form", files=_files(blank_pdf, blank_data))
        assert r.status_code == 429


# --------------------------------------------------------------------------- #
# /generate-data-json — gated separately from the sync endpoints
# --------------------------------------------------------------------------- #

class TestGenerateRateLimit:

    @pytest.fixture(autouse=True)
    def _check_env(self):
        if not _tight_limit_in_effect("RATE_LIMIT_GENERATE"):
            pytest.skip("RATE_LIMIT_GENERATE not set tight; skipping")

    def test_429_after_quota_exhausted(self, http, blank_pdf):
        n = int(os.environ["RATE_LIMIT_GENERATE"].split("/")[0])
        files = [("questionnaire_file",
                  (blank_pdf.name, blank_pdf.read_bytes(), "application/pdf"))]
        for i in range(n):
            r = http.post("/generate-data-json", files=files)
            assert r.status_code in (202, 400, 415), \
                f"request {i} failed: {r.status_code}"
        r = http.post("/generate-data-json", files=files)
        assert r.status_code == 429


# --------------------------------------------------------------------------- #
# Endpoints that should NOT be rate-limited
# --------------------------------------------------------------------------- #

class TestUnlimitedEndpoints:

    @pytest.fixture(autouse=True)
    def _check_env(self):
        # Only run when the throttled endpoints have tight limits — otherwise
        # this test isn't proving anything.
        if not _tight_limit_in_effect("RATE_LIMIT_TO_ACROFORM"):
            pytest.skip("not in a tight-limit env; nothing to compare against")

    def test_healthz_not_rate_limited(self, http):
        """30 calls in a row to /healthz should all succeed; this endpoint
        carries no @limiter.limit decorator."""
        for i in range(30):
            r = http.get("/healthz")
            assert r.status_code == 200, f"call {i} got {r.status_code}"

    def test_jobs_polling_not_rate_limited(self, http):
        """Status polling has to be free — clients hit it at 1Hz."""
        # 30 polls of an unknown job (404) should all return 404, not 429.
        for i in range(30):
            r = http.get("/jobs/nonexistent-id-for-rate-test")
            assert r.status_code == 404, f"call {i} got {r.status_code}"
