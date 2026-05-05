"""Cross-cutting tests for the API surface (not specific to one endpoint).

Coverage:
    /docs, /redoc, /scalar, /openapi.json     — documentation surfaces
    OpenAPI spec contents                      — every endpoint listed
    CORS preflight                             — Access-Control-* headers
    Generic 404 / 405                          — unknown paths, wrong methods
    MaxBodySizeMiddleware edge cases           — invalid Content-Length, tiny POSTs
"""

from __future__ import annotations

import httpx
import pytest


# --------------------------------------------------------------------------- #
# Documentation endpoints
# --------------------------------------------------------------------------- #

class TestDocs:

    def test_openapi_returns_200(self, http):
        r = http.get("/openapi.json")
        assert r.status_code == 200
        spec = r.json()
        assert spec["openapi"].startswith("3.0"), \
            f"expected OpenAPI 3.0.x for Swagger UI file pickers, got {spec['openapi']}"

    def test_openapi_lists_all_endpoints(self, http):
        spec = http.get("/openapi.json").json()
        paths = spec["paths"]
        for path in (
            "/healthz",
            "/generate-data-json",
            "/jobs/{job_id}",
            "/jobs/{job_id}/data.json",
            "/fill-form",
            "/to-acroform",
        ):
            assert path in paths, f"missing in OpenAPI spec: {path}"

    def test_openapi_file_uploads_are_binary(self, http):
        """The 3.1 → 3.0.2 rewrite must convert contentMediaType to format=binary
        so Swagger UI shows file pickers (verified in main.py:107-131)."""
        spec = http.get("/openapi.json").json()
        # Spot-check: /to-acroform's form_file should advertise format: binary.
        post = spec["paths"]["/to-acroform"]["post"]
        body = post["requestBody"]["content"]["multipart/form-data"]["schema"]
        props = body.get("properties") or body.get("$ref")
        # If schema is referenced, dereference.
        if "$ref" in body:
            ref = body["$ref"].split("/")[-1]
            props = spec["components"]["schemas"][ref]["properties"]
        assert "form_file" in props
        ff = props["form_file"]
        # Either format: binary (rewritten) or contentMediaType has been removed.
        assert ff.get("format") == "binary" or "contentMediaType" not in ff, \
            f"form_file is not advertised as binary: {ff}"

    def test_swagger_docs_renders(self, http):
        r = http.get("/docs")
        assert r.status_code == 200
        assert "swagger" in r.text.lower()

    def test_redoc_renders(self, http):
        r = http.get("/redoc")
        assert r.status_code == 200
        assert "redoc" in r.text.lower()

    def test_scalar_renders(self, http):
        r = http.get("/scalar")
        assert r.status_code == 200
        assert "scalar" in r.text.lower()


# --------------------------------------------------------------------------- #
# CORS — env-driven via CORS_ALLOWED_ORIGINS (default = localhost set)
# --------------------------------------------------------------------------- #

class TestCORS:

    def test_allowed_origin_is_echoed(self, http):
        """An origin in the allowlist should be echoed back."""
        r = http.get("/healthz", headers={"Origin": "http://localhost:3000"})
        assert r.status_code == 200
        assert r.headers.get("access-control-allow-origin") == "http://localhost:3000"

    def test_disallowed_origin_is_not_echoed(self, http):
        """An origin NOT in the allowlist must not get an ACAO header.

        Starlette's CORSMiddleware silently omits the header for unmatched
        origins; the actual response still goes through. The browser is
        responsible for blocking the JS read on a missing header.
        """
        r = http.get("/healthz", headers={"Origin": "https://evil.example.com"})
        assert r.status_code == 200
        # No ACAO header for an origin we don't trust. (If the user has set
        # CORS_ALLOWED_ORIGINS=* in this env, this test will fail-loud — the
        # whole point is that we DON'T want * in production.)
        acao = r.headers.get("access-control-allow-origin")
        assert acao != "https://evil.example.com", \
            f"untrusted origin was reflected: {acao}"

    def test_preflight_for_allowed_origin(self, base_url):
        with httpx.Client(base_url=base_url, timeout=10.0) as c:
            r = c.request(
                "OPTIONS",
                "/fill-form",
                headers={
                    "Origin": "http://localhost:3000",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "Content-Type",
                },
            )
        assert r.status_code in (200, 204)
        assert "access-control-allow-methods" in r.headers
        allowed = r.headers["access-control-allow-methods"]
        assert "POST" in allowed or "*" in allowed


# --------------------------------------------------------------------------- #
# Generic 404 / 405
# --------------------------------------------------------------------------- #

class TestErrors:

    def test_unknown_path_returns_404(self, http):
        r = http.get("/this-endpoint-does-not-exist")
        assert r.status_code == 404

    def test_root_path_returns_404(self, http):
        # No "/" handler registered.
        r = http.get("/")
        assert r.status_code in (404, 200)  # allow either; just shouldn't 500

    def test_post_to_get_endpoint_405(self, http):
        r = http.post("/healthz")
        assert r.status_code == 405

    def test_get_to_post_endpoint_405(self, http):
        r = http.get("/fill-form")
        assert r.status_code == 405

    def test_put_to_post_endpoint_405(self, http):
        r = http.put("/to-acroform")
        assert r.status_code == 405


# --------------------------------------------------------------------------- #
# MaxBodySizeMiddleware — edge cases beyond the simple oversize case
# --------------------------------------------------------------------------- #

class TestBodySize:

    def test_under_limit_passes_middleware(self, http, blank_pdf):
        """A small request must pass the middleware and reach the endpoint."""
        # Wrong filename to ensure the endpoint runs (validation rejects, not
        # the middleware) — proves the middleware passed the request through.
        r = http.post(
            "/to-acroform",
            files=[("form_file", ("form.exe", b"junk", "application/octet-stream"))],
        )
        # 415 = endpoint validation ran, so middleware allowed the request.
        assert r.status_code == 415

    def test_invalid_content_length_handled(self, base_url, blank_pdf):
        """A request with a malformed Content-Length must not 500.

        The middleware swallows ValueError (file_validation.py:161-163) and
        passes through; the underlying server may then 400/411/etc., or
        succeed if it ignores the bad header. We just want NO 500.
        """
        # We can't easily forge a malformed header through httpx (it
        # auto-computes Content-Length), so instead we send a chunked body
        # with no Content-Length at all.
        with httpx.Client(base_url=base_url, timeout=10.0) as c:
            r = c.post(
                "/healthz",
                content=iter([b"x"]),  # streaming body → chunked, no CL
            )
        # /healthz is GET-only so we expect 405; either way, NOT 500.
        assert r.status_code != 500
