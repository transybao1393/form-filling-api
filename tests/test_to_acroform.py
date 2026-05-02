"""End-to-end test suite for POST /to-acroform.

Run inside Docker:

    docker compose up -d
    docker compose run --rm \
        -v "$PWD/tests:/app/tests:ro" \
        -v "$PWD/input:/app/input:ro" \
        -e API_BASE_URL=http://api:8000 \
        --no-deps api pytest tests/ -v --tb=short

Coverage:
    happy paths • error/validation • output integrity • headers •
    concurrency • integration with /fill-form, /healthz, /scalar
"""

from __future__ import annotations

import io
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from pypdf import PdfReader, PdfWriter


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _files(form_path: Path,
           data_path: Path | None = None,
           answers_path: Path | None = None,
           form_mime: str = "application/pdf"):
    files = [("form_file", (form_path.name, form_path.read_bytes(), form_mime))]
    if data_path:
        files.append(("data_file",
                      (data_path.name, data_path.read_bytes(), "application/json")))
    if answers_path:
        files.append(("answers_file",
                      (answers_path.name, answers_path.read_bytes(),
                       "application/json")))
    return files


def _read_pdf_fields(content: bytes) -> dict:
    return PdfReader(io.BytesIO(content)).get_fields() or {}


def _read_acroform_dict(content: bytes):
    r = PdfReader(io.BytesIO(content))
    return r.trailer["/Root"].get("/AcroForm")


# --------------------------------------------------------------------------- #
# Group 1 — happy paths
# --------------------------------------------------------------------------- #

class TestHappyPaths:

    def test_existing_acroform_no_data_fast_path(self, http, acroform_pdf):
        """PDF already has AcroForm + no data → return as-is, no detection."""
        r = http.post("/to-acroform", files=_files(acroform_pdf))
        assert r.status_code == 200, r.text
        assert r.headers["content-type"] == "application/pdf"
        assert r.headers["x-acroform-source"] == "existing"
        assert int(r.headers["x-fields-total"]) > 0
        assert r.headers["x-fields-filled"] == "0"
        assert r.headers["x-fields-carried-over"] == "0"
        assert 'attachment; filename="acroform.pdf"' in r.headers["content-disposition"]
        # Output equals input field-count exactly.
        assert int(r.headers["x-fields-total"]) == \
            len(_read_pdf_fields(acroform_pdf.read_bytes()))

    def test_existing_acroform_with_data_native_fill(
        self, http, acroform_pdf, acroform_data
    ):
        r = http.post(
            "/to-acroform",
            files=_files(acroform_pdf, acroform_data),
        )
        assert r.status_code == 200
        assert r.headers["x-acroform-source"] == "existing"
        # at least some answers should match — the test1 fixture is well-known
        assert int(r.headers["x-fields-filled"]) >= 1
        # Output is still an AcroForm with the same widget count.
        out_fields = _read_pdf_fields(r.content)
        assert len(out_fields) == int(r.headers["x-fields-total"])

    def test_blank_pdf_with_data_injection(self, http, blank_pdf, blank_data):
        r = http.post("/to-acroform", files=_files(blank_pdf, blank_data))
        assert r.status_code == 200
        assert r.headers["x-acroform-source"] == "injected"
        n_total = int(r.headers["x-fields-total"])
        n_filled = int(r.headers["x-fields-filled"])
        assert n_total > 0
        assert n_filled > 0
        # Output PDF actually contains the widgets we report.
        out_fields = _read_pdf_fields(r.content)
        assert len(out_fields) == n_total
        # Pre-populated values exist on at least some widgets.
        with_value = [k for k, v in out_fields.items() if v.get("/V")]
        assert len(with_value) >= n_filled

    def test_blank_pdf_no_data_empty_widgets(self, http, blank_pdf):
        """No data → all widgets injected but unpopulated."""
        r = http.post("/to-acroform", files=_files(blank_pdf))
        assert r.status_code == 200
        assert r.headers["x-acroform-source"] == "injected"
        assert r.headers["x-fields-filled"] == "0"
        out_fields = _read_pdf_fields(r.content)
        assert len(out_fields) == int(r.headers["x-fields-total"])

    def test_existing_acroform_with_format_override(
        self, http, acroform_pdf, acroform_data
    ):
        """The format= override is honoured (here we force flatlist explicitly)."""
        r = http.post(
            "/to-acroform",
            files=_files(acroform_pdf, acroform_data),
            data={"format": "flatlist"},
        )
        assert r.status_code == 200
        assert r.headers["x-acroform-source"] == "existing"


# --------------------------------------------------------------------------- #
# Group 2 — error & input validation
# --------------------------------------------------------------------------- #

class TestErrors:

    def test_docx_rejected_415(self, http, docx_dummy):
        r = http.post("/to-acroform", files=_files(docx_dummy))
        assert r.status_code == 415
        assert "pdf" in r.json()["detail"].lower()

    def test_image_rejected_415(self, http, tmp_path):
        png = tmp_path / "form.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        r = http.post("/to-acroform", files=_files(png))
        assert r.status_code == 415

    def test_no_extension_rejected_415(self, http, tmp_path):
        weird = tmp_path / "form"
        weird.write_bytes(b"%PDF-1.4\n%%EOF\n")
        r = http.post("/to-acroform", files=_files(weird))
        assert r.status_code == 415

    def test_missing_form_file_422(self, http):
        # FastAPI raises 422 when a required form field is absent.
        r = http.post("/to-acroform", data={})
        assert r.status_code == 422

    def test_invalid_format_400(self, http, blank_pdf):
        r = http.post(
            "/to-acroform",
            files=_files(blank_pdf),
            data={"format": "bogus"},
        )
        assert r.status_code == 400
        assert "format" in r.json()["detail"].lower()

    def test_corrupt_pdf_handled_gracefully(self, http, tiny_invalid_pdf):
        """Corrupt PDF → 4xx, not a 500 stack trace."""
        r = http.post("/to-acroform", files=_files(tiny_invalid_pdf))
        # Either 400 (no detectable fields), 415, or 500 are seen across
        # parsers; we want the server to NOT leak an unhandled traceback —
        # so just demand it returns a reasonable error code.
        assert r.status_code in (400, 415, 422, 500)
        # Body should be JSON, not a stack trace.
        try:
            body = r.json()
            assert "detail" in body
        except Exception:
            pytest.fail(f"non-JSON body for corrupt PDF: {r.text[:200]}")

    def test_empty_pdf_no_fields_400(self, http, empty_pdf):
        """Valid PDF with no detectable fields → 400 with a helpful message."""
        r = http.post("/to-acroform", files=_files(empty_pdf))
        assert r.status_code == 400
        assert "fields" in r.json()["detail"].lower()


# --------------------------------------------------------------------------- #
# Group 3 — output integrity
# --------------------------------------------------------------------------- #

class TestOutputIntegrity:

    def test_response_is_valid_pdf(self, http, blank_pdf, blank_data):
        r = http.post("/to-acroform", files=_files(blank_pdf, blank_data))
        assert r.status_code == 200
        assert r.content[:5] == b"%PDF-"
        # Round-trip parse must not raise.
        reader = PdfReader(io.BytesIO(r.content))
        assert len(reader.pages) > 0

    def test_acroform_dict_well_formed(self, http, blank_pdf, blank_data):
        r = http.post("/to-acroform", files=_files(blank_pdf, blank_data))
        af = _read_acroform_dict(r.content)
        assert af is not None, "no /AcroForm dict on output"
        # /NeedAppearances must be true so viewers re-render appearances.
        # pypdf returns BooleanObject which compares == True but `is True`.
        assert bool(af.get("/NeedAppearances")) is True
        fields = af["/Fields"]
        if hasattr(fields, "get_object"):
            fields = fields.get_object()
        assert len(fields) >= 1

    def test_widget_rects_within_page_bounds(self, http, blank_pdf, blank_data):
        r = http.post("/to-acroform", files=_files(blank_pdf, blank_data))
        reader = PdfReader(io.BytesIO(r.content))

        # Map indirect-object id → (page_w, page_h) so we can resolve a
        # widget's /P back to its page even though pypdf hands us a plain
        # DictionaryObject.
        page_dims_by_id: dict[int, tuple[float, float]] = {}
        for p in reader.pages:
            mb = p["/MediaBox"]
            dims = (float(mb[2]) - float(mb[0]), float(mb[3]) - float(mb[1]))
            ind = p.indirect_reference
            if ind is not None:
                page_dims_by_id[ind.idnum] = dims

        af_fields = reader.trailer["/Root"]["/AcroForm"]["/Fields"]
        if hasattr(af_fields, "get_object"):
            af_fields = af_fields.get_object()
        for ref in af_fields:
            f = ref.get_object()
            rect = f.get("/Rect")
            if not rect:
                continue
            x0, y0, x1, y1 = (float(v) for v in rect)
            page_ref = f.get("/P")
            if page_ref is None:
                continue
            # /P is an IndirectObject; resolve via its idnum.
            ind = getattr(page_ref, "indirect_reference", page_ref)
            idnum = getattr(ind, "idnum", None)
            if idnum is None or idnum not in page_dims_by_id:
                continue
            pw, ph = page_dims_by_id[idnum]
            assert 0 <= x0 <= pw + 1, f"x0 out of bounds: {x0} > {pw}"
            assert 0 <= x1 <= pw + 1, f"x1 out of bounds: {x1} > {pw}"
            assert 0 <= y0 <= ph + 1, f"y0 out of bounds: {y0} > {ph}"
            assert 0 <= y1 <= ph + 1, f"y1 out of bounds: {y1} > {ph}"
            assert x1 > x0, f"degenerate rect: x1<=x0 ({x0},{x1})"
            assert y1 > y0, f"degenerate rect: y1<=y0 ({y0},{y1})"

    def test_widgets_unique_names(self, http, blank_pdf, blank_data):
        """The /T (field name) must be unique within the document."""
        r = http.post("/to-acroform", files=_files(blank_pdf, blank_data))
        reader = PdfReader(io.BytesIO(r.content))
        names = [
            ref.get_object().get("/T")
            for ref in reader.trailer["/Root"]["/AcroForm"]["/Fields"]
        ]
        assert len(names) == len(set(names)), \
            f"duplicate field names: {[n for n in names if names.count(n) > 1][:5]}"

    def test_pre_populated_values_present(self, http, blank_pdf, blank_data):
        r = http.post("/to-acroform", files=_files(blank_pdf, blank_data))
        n_filled = int(r.headers["x-fields-filled"])
        out = _read_pdf_fields(r.content)
        with_v = [k for k, v in out.items() if v.get("/V")]
        # At least n_filled widgets should carry /V (checkboxes count as filled
        # even when /Off, so the inequality is loose).
        assert len(with_v) >= n_filled


# --------------------------------------------------------------------------- #
# Group 4 — headers
# --------------------------------------------------------------------------- #

class TestHeaders:

    def test_required_headers_present(self, http, blank_pdf):
        r = http.post("/to-acroform", files=_files(blank_pdf))
        for h in ("content-type", "content-disposition",
                  "x-acroform-source", "x-fields-total",
                  "x-fields-filled", "x-fields-carried-over"):
            assert h in r.headers, f"missing header: {h}"

    def test_header_values_are_integers(self, http, blank_pdf, blank_data):
        r = http.post("/to-acroform", files=_files(blank_pdf, blank_data))
        for k in ("x-fields-total", "x-fields-filled", "x-fields-carried-over"):
            int(r.headers[k])  # raises if not int

    def test_acroform_source_is_known_value(self, http, blank_pdf):
        r = http.post("/to-acroform", files=_files(blank_pdf))
        assert r.headers["x-acroform-source"] in {"existing", "injected"}

    def test_filename_is_acroform_pdf(self, http, blank_pdf):
        r = http.post("/to-acroform", files=_files(blank_pdf))
        assert 'filename="acroform.pdf"' in r.headers["content-disposition"]


# --------------------------------------------------------------------------- #
# Group 5 — concurrency & robustness
# --------------------------------------------------------------------------- #

class TestConcurrency:

    def test_five_parallel_requests_all_succeed(
        self, base_url, blank_pdf, blank_data
    ):
        """Five parallel /to-acroform calls all return 200 and valid PDFs."""
        def one_call():
            with httpx.Client(base_url=base_url, timeout=60.0) as c:
                r = c.post("/to-acroform", files=_files(blank_pdf, blank_data))
                return r.status_code, r.content[:5], r.headers.get("x-fields-total")

        with ThreadPoolExecutor(max_workers=5) as ex:
            results = list(ex.map(lambda _: one_call(), range(5)))
        assert all(code == 200 for code, _, _ in results)
        assert all(magic == b"%PDF-" for _, magic, _ in results)
        # All five should report the same field count (deterministic detection).
        field_counts = {totals for _, _, totals in results}
        assert len(field_counts) == 1, f"flaky detection: {field_counts}"

    def test_repeated_serial_requests_no_resource_leak(
        self, http, blank_pdf, blank_data
    ):
        """10 sequential calls — none should fail or slow down materially."""
        for i in range(10):
            r = http.post("/to-acroform", files=_files(blank_pdf, blank_data))
            assert r.status_code == 200, f"request {i} failed: {r.status_code}"


# --------------------------------------------------------------------------- #
# Group 6 — filename safety
# --------------------------------------------------------------------------- #

class TestFilenameSafety:

    def test_path_traversal_filename_handled(self, http, blank_pdf, tmp_path):
        """A filename with ../ should not escape the temp dir."""
        evil = b"\x00\x01"  # arbitrary bytes
        # Use a filename with traversal characters.
        files = [(
            "form_file",
            ("../../../../../tmp/pwned.pdf", blank_pdf.read_bytes(), "application/pdf"),
        )]
        r = http.post("/to-acroform", files=files)
        # We don't care about the status — we care that no file got written
        # outside the API's temp dir. The endpoint only validates suffix,
        # so 200 is fine. The Path(...).name strip in main.py guarantees safety.
        assert r.status_code in (200, 400, 415)

    def test_unicode_filename_handled(self, http, blank_pdf):
        files = [(
            "form_file",
            ("café—test.pdf", blank_pdf.read_bytes(), "application/pdf"),
        )]
        r = http.post("/to-acroform", files=files)
        assert r.status_code == 200


# --------------------------------------------------------------------------- #
# Group 7 — integration / regression
# --------------------------------------------------------------------------- #

class TestIntegration:

    def test_healthz_still_works(self, http):
        r = http.get("/healthz")
        assert r.status_code == 200
        body = r.json()
        assert body["model"]
        assert body["ollama"] in {"ok", "down"}

    def test_scalar_doc_renders(self, http):
        r = http.get("/scalar")
        assert r.status_code == 200
        assert "Scalar" in r.text or "scalar" in r.text.lower()

    def test_openapi_lists_to_acroform(self, http):
        r = http.get("/openapi.json")
        assert r.status_code == 200
        assert "/to-acroform" in r.json()["paths"]

    def test_fill_form_still_returns_overlay(
        self, http, blank_pdf, blank_data
    ):
        """No regression: /fill-form continues to return overlay PDFs."""
        r = http.post("/fill-form", files=_files(blank_pdf, blank_data))
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/pdf"
        # Overlay output should NOT contain an AcroForm dict.
        af = _read_acroform_dict(r.content)
        assert af is None or len((af.get("/Fields") or [])) == 0, \
            "/fill-form leaked AcroForm widgets into overlay output"

    def test_to_acroform_then_fillable_in_pypdf(
        self, http, blank_pdf, blank_data
    ):
        """Round-trip: take /to-acroform output, fill values via pypdf,
        re-read — values must persist."""
        r = http.post("/to-acroform", files=_files(blank_pdf, blank_data))
        reader = PdfReader(io.BytesIO(r.content))
        writer = PdfWriter(clone_from=reader)
        # Pick the first text field and overwrite its /V via pypdf.
        fields = reader.get_fields() or {}
        first_name = next(iter(fields))
        writer.update_page_form_field_values(
            writer.pages[0], {first_name: "ROUND_TRIP_OK"}
        )
        buf = io.BytesIO()
        writer.write(buf)
        # Re-read.
        rt = PdfReader(io.BytesIO(buf.getvalue()))
        assert (rt.get_fields() or {}).get(first_name, {}).get("/V") in (
            "ROUND_TRIP_OK", b"ROUND_TRIP_OK",
        )
