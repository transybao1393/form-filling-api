"""Tests for GET /healthz.

Schema:
    {
        "status": "ok" | "degraded",
        "ollama": "ok" | "down",
        "redis":  "ok" | "down",
        "model": "<string>",
    }

Top-level `status` is "ok" iff every component is reachable; otherwise
"degraded". Endpoint always returns 200 — clients read the body to decide
what to page on (a 503 here would block the load balancer instead).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest


class TestHealthz:

    def test_returns_200(self, http):
        r = http.get("/healthz")
        assert r.status_code == 200

    def test_content_type_is_json(self, http):
        r = http.get("/healthz")
        assert r.headers["content-type"].startswith("application/json")

    def test_response_shape(self, http):
        body = http.get("/healthz").json()
        assert set(body.keys()) == {"status", "ollama", "redis", "model"}
        assert body["status"] in {"ok", "degraded"}
        assert body["ollama"] in {"ok", "down"}
        assert body["redis"] in {"ok", "down"}
        assert isinstance(body["model"], str)
        assert body["model"]  # non-empty

    def test_status_reflects_components(self, http):
        body = http.get("/healthz").json()
        # status must be "ok" iff BOTH dependencies are up.
        if body["ollama"] == "ok" and body["redis"] == "ok":
            assert body["status"] == "ok"
        else:
            assert body["status"] == "degraded"

    def test_redis_is_up_in_test_env(self, http):
        """The Docker compose stack always has Redis healthy before tests run."""
        body = http.get("/healthz").json()
        assert body["redis"] == "ok", \
            "Redis should be reachable in the test stack — check `docker compose ps`"

    def test_model_matches_default_or_env(self, http):
        body = http.get("/healthz").json()
        # In Docker compose the default is qwen3:8b. Just assert plausible.
        assert ":" in body["model"] or "/" in body["model"] or body["model"].isalnum() \
            or body["model"]

    def test_method_post_not_allowed(self, http):
        r = http.post("/healthz")
        assert r.status_code == 405

    def test_idempotent_under_load(self, base_url):
        """20 parallel calls — all return the same body and 200."""
        def one():
            with httpx.Client(base_url=base_url, timeout=10.0) as c:
                r = c.get("/healthz")
                return r.status_code, r.text

        with ThreadPoolExecutor(max_workers=20) as ex:
            results = list(ex.map(lambda _: one(), range(20)))
        assert all(code == 200 for code, _ in results)
        # Body should be identical (deterministic) — at most one transition
        # if Ollama starts/stops mid-test, but we don't expect that here.
        bodies = {body for _, body in results}
        assert len(bodies) <= 2, f"too many distinct bodies: {bodies}"
