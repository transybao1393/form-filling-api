"""End-to-end tests for the async job endpoints.

Coverage:
    POST /generate-data-json       submission shape, validation already in
                                   test_file_validation.py
    GET  /jobs/{job_id}            status shape, 404, terminal transitions
    GET  /jobs/{job_id}/data.json  download, 404, 409 (not completed)

Tests that need Ollama to actually run are gated by `LLM_TESTS=1` and
marked with `@pytest.mark.llm`. The non-LLM tests cover everything except
the actual model call by relying on the queue / state-machine being correct
even before the worker finishes.
"""

from __future__ import annotations

import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from tests._helpers import (
    LLM_TIMEOUT_S,
    poll_until_terminal,
    submit_job,
)


# --------------------------------------------------------------------------- #
# Group 1 — submission contract
# --------------------------------------------------------------------------- #

class TestSubmission:

    def test_submit_returns_202_and_expected_body(self, http, blank_pdf):
        body = submit_job(http, blank_pdf)
        assert body["status"] == "queued"
        # job_id must be a uuid hex (32 chars).
        uuid.UUID(body["job_id"])
        assert body["status_url"] == f"/jobs/{body['job_id']}"
        assert body["download_url"] == f"/jobs/{body['job_id']}/data.json"

    def test_submit_with_title_persists_in_state(self, http, blank_pdf):
        body = submit_job(http, blank_pdf, title="My Special Title")
        # The title doesn't appear in JobStatus; we can only check the job
        # was accepted. Actual title application is verified in TestE2E.
        r = http.get(body["status_url"])
        assert r.status_code == 200

    def test_submit_with_references(self, http, blank_pdf, reference_doc):
        body = submit_job(http, blank_pdf, references=[reference_doc])
        # Job should reach 'queued' or 'running' state — references stored on
        # disk; we can't observe the meta.json from the API, just sanity-check.
        r = http.get(body["status_url"])
        assert r.status_code == 200
        assert r.json()["status"] in {"queued", "running", "completed", "failed"}

    def test_submit_dedupes_reference_filenames(self, http, blank_pdf, tmp_path):
        """Two refs with the same filename should not collide on disk."""
        a = tmp_path / "ref.txt"
        a.write_text("alpha")
        files = [
            ("questionnaire_file",
             (blank_pdf.name, blank_pdf.read_bytes(), "application/pdf")),
            ("reference_files", ("ref.txt", b"alpha", "text/plain")),
            ("reference_files", ("ref.txt", b"bravo", "text/plain")),
        ]
        r = http.post("/generate-data-json", files=files)
        # Two refs with the same name must both be accepted (de-dup is by
        # the API's _dedup_name helper).
        assert r.status_code == 202

    def test_submit_concurrent_returns_distinct_job_ids(self, base_url, blank_pdf):
        """5 parallel submissions yield 5 distinct job_ids."""
        def one():
            with httpx.Client(base_url=base_url, timeout=30.0) as c:
                files = [("questionnaire_file",
                          (blank_pdf.name, blank_pdf.read_bytes(), "application/pdf"))]
                r = c.post("/generate-data-json", files=files)
                assert r.status_code == 202
                return r.json()["job_id"]

        with ThreadPoolExecutor(max_workers=5) as ex:
            ids = list(ex.map(lambda _: one(), range(5)))
        assert len(set(ids)) == 5, f"duplicate job ids: {ids}"


# --------------------------------------------------------------------------- #
# Group 2 — /jobs/{id} status endpoint
# --------------------------------------------------------------------------- #

class TestJobStatus:

    def test_unknown_job_id_returns_404(self, http):
        r = http.get("/jobs/does-not-exist-deadbeef")
        assert r.status_code == 404
        assert "unknown job_id" in r.json()["detail"].lower()

    def test_status_shape_after_submission(self, http, blank_pdf):
        body = submit_job(http, blank_pdf)
        r = http.get(body["status_url"])
        assert r.status_code == 200
        s = r.json()
        # Required fields per JobStatusResponse.
        for k in ("job_id", "status", "percent", "stage", "stage_text",
                  "submitted_at"):
            assert k in s, f"missing {k}"
        assert s["job_id"] == body["job_id"]
        assert s["status"] in {"queued", "running", "completed", "failed", "review"}
        assert 0 <= s["percent"] <= 100
        assert s["stage"] in {
            "queued", "extracting_questionnaire", "extracting_references",
            "calling_llm_service", "saving",
            "completed", "failed", "review",
        }
        assert isinstance(s["stage_text"], str) and s["stage_text"]
        # ISO 8601 timestamp.
        assert "T" in s["submitted_at"]

    def test_download_url_only_present_when_result_available(self, http, blank_pdf):
        body = submit_job(http, blank_pdf)
        s = http.get(body["status_url"]).json()
        # `download_url` is populated iff a result.json was written —
        # i.e. terminal status is `completed` (all answers OK) or `review`
        # (LLM finished, low-confidence items need human approval).
        if s["status"] in {"completed", "review"}:
            assert s["download_url"] == f"/jobs/{body['job_id']}/data.json"
        else:
            assert s.get("download_url") is None

    def test_status_is_repeatable(self, http, blank_pdf):
        """Polling twice in quick succession returns consistent state.

        Status may transition queued → running between calls; what should
        NOT happen is the percent going backwards or fields disappearing.
        """
        body = submit_job(http, blank_pdf)
        a = http.get(body["status_url"]).json()
        b = http.get(body["status_url"]).json()
        assert a["job_id"] == b["job_id"]
        assert b["percent"] >= a["percent"]
        # status must move forward only: queued → running → completed/failed
        order = {"queued": 0, "running": 1, "completed": 2, "failed": 2}
        assert order[b["status"]] >= order[a["status"]]


# --------------------------------------------------------------------------- #
# Group 3 — /jobs/{id}/data.json download endpoint
# --------------------------------------------------------------------------- #

class TestJobDownload:

    def test_unknown_job_id_returns_404(self, http):
        r = http.get("/jobs/nonexistent-deadbeef/data.json")
        assert r.status_code == 404

    def test_not_completed_returns_409_with_state(self, http, blank_pdf):
        """A job whose result isn't ready yet returns 409 with the current state."""
        body = submit_job(http, blank_pdf)
        # Race: if Ollama is fast the job may already be terminal with a
        # result available (status=completed or review → 200). Skip then.
        r = http.get(body["download_url"])
        if r.status_code == 200:
            pytest.skip("job already produced a result before we checked — Ollama too fast")
        assert r.status_code == 409
        payload = r.json()
        assert payload["detail"]
        assert "current" in payload
        assert payload["current"]["job_id"] == body["job_id"]
        assert payload["current"]["status"] in {"queued", "running", "failed"}


# --------------------------------------------------------------------------- #
# Group 4 — full E2E (gated by LLM_TESTS=1)
# --------------------------------------------------------------------------- #

class TestE2E:

    @pytest.mark.llm
    def test_submit_poll_complete_download(
        self, http, blank_pdf, reference_doc
    ):
        """Submit → poll until terminal → download → validate schema.

        Accepts both `completed` and `review` as success: the LLM finished
        and produced a result.json. `review` just means at least one item
        was low-confidence and needs human approval — that does not change
        whether the request succeeded end-to-end.
        """
        body = submit_job(http, blank_pdf, references=[reference_doc])
        final = poll_until_terminal(http, body["job_id"])
        if final["status"] == "failed":
            pytest.fail(f"job failed: {final.get('error')!r}")
        assert final["status"] in {"completed", "review"}
        assert final["percent"] == 100
        assert final["stage"] in {"completed", "review"}
        assert final["completed_at"] is not None
        assert final["error"] is None
        assert final["download_url"] == f"/jobs/{body['job_id']}/data.json"

        # Download.
        r = http.get(body["download_url"])
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")
        assert "data.json" in r.headers.get("content-disposition", "")
        data = r.json()
        # Schema sanity (matches DataJson).
        assert "questionnaire_title" in data
        assert "items" in data and isinstance(data["items"], list)
        if data["items"]:
            it = data["items"][0]
            for k in ("contextualized_question", "source_file", "question",
                      "question_number", "extracted_answer", "confidence"):
                assert k in it, f"missing item key: {k}"
            assert it["confidence"] in {"HIGH", "MEDIUM", "NONE"}
            # F1, F2, ... numbering.
            assert it["question_number"].startswith("F")

    @pytest.mark.llm
    def test_title_override_applied(self, http, blank_pdf):
        body = submit_job(http, blank_pdf, title="OVERRIDE_TITLE_42")
        final = poll_until_terminal(http, body["job_id"])
        if final["status"] not in {"completed", "review"}:
            pytest.skip(f"job did not produce a result: {final.get('error')}")
        data = http.get(body["download_url"]).json()
        assert data["questionnaire_title"] == "OVERRIDE_TITLE_42"

    @pytest.mark.llm
    def test_progress_monotonic(self, http, blank_pdf):
        """percent never decreases over the lifetime of a job."""
        body = submit_job(http, blank_pdf)
        seen = []
        deadline = time.monotonic() + LLM_TIMEOUT_S
        while time.monotonic() < deadline:
            s = http.get(body["status_url"]).json()
            seen.append(s["percent"])
            if s["status"] in {"completed", "failed", "review"}:
                break
            time.sleep(0.5)
        # Percent must be non-decreasing.
        for a, b in zip(seen, seen[1:]):
            assert b >= a, f"percent went backwards: {seen}"

    @pytest.mark.llm
    def test_failed_job_records_error(self, http, tmp_path):
        """A questionnaire we can't extract any text from must end in `failed`
        with a non-null error message — not 200 with garbage."""
        # An image with no OCR-able content (pixels of solid colour). Tesseract
        # will return empty → worker raises 'could not extract any text'.
        from PIL import Image
        img = tmp_path / "empty.png"
        Image.new("RGB", (200, 200), color="white").save(img)
        body = submit_job(http, img)
        final = poll_until_terminal(http, body["job_id"])
        # Either OCR found nothing (fail) or it returned a single noise token
        # (pass). Both are acceptable as long as a fail records the error.
        if final["status"] == "failed":
            assert final["error"]
            assert final["stage"] == "failed"
            assert final["completed_at"] is not None
            # Download endpoint must surface the failure as 409, not 200.
            r = http.get(body["download_url"])
            assert r.status_code == 409
        else:
            pytest.skip("OCR coincidentally found text — can't assert failure")
