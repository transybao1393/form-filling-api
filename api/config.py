"""Runtime configuration read from environment variables."""

from __future__ import annotations

import os
from pathlib import Path


# --- LLM orchestration service ---------------------------------------------
# The main app no longer talks to Ollama directly. All LLM concerns (prompts,
# /api/chat, parsing, normalization) live in the host-native `llm_service`
# process (see llm_service/README.md). From inside docker-compose this is
# reached via host.docker.internal:11500.
LLM_SERVICE_URL: str = os.getenv(
    "LLM_SERVICE_URL", "http://localhost:11500"
).rstrip("/")

# Per-request HTTP timeout (seconds). Generous because the underlying
# Ollama call can take 30–90s on Apple Silicon and we add HTTP overhead.
LLM_SERVICE_TIMEOUT: float = float(os.getenv("LLM_SERVICE_TIMEOUT", "360"))

# --- arq job queue ---------------------------------------------------------
REDIS_HOST: str = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT: int = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DATABASE: int = int(os.getenv("REDIS_DATABASE", "0"))

# Where job state, uploads, and result files live. Each job gets its own
# subdirectory: JOBS_DIR/<job_id>/{state.json, meta.json, uploads/, result.json}.
# Inside Docker this is overridden to /var/lib/form-pipeline/jobs (mounted volume).
JOBS_DIR: Path = Path(os.getenv("JOBS_DIR", "~/.form_pipeline/jobs")).expanduser()

# Garbage-collect job directories older than this. Default: 7 days.
JOB_TTL_HOURS: int = int(os.getenv("JOB_TTL_HOURS", "168"))

# --- file upload limits ----------------------------------------------------
# Hard global cap on a single HTTP request body, enforced by middleware as
# soon as the Content-Length header arrives. Defends against OOM from large
# multipart uploads. 100 MB default — bump for image-heavy use cases.
MAX_REQUEST_BYTES: int = int(os.getenv("MAX_REQUEST_BYTES", str(100 * 1024 * 1024)))

# Default per-file size cap (MB) when an endpoint doesn't override it.
# Most endpoints take a few-MB form + smaller JSON, so 50 MB is generous.
MAX_UPLOAD_MB: int = int(os.getenv("MAX_UPLOAD_MB", "50"))

# --- CORS --------------------------------------------------------------------
# Comma-separated list of origins permitted to call the API from a browser.
# Empty / unset → safe localhost defaults (dev-friendly). In production, set
# to the actual frontend origin(s), e.g.:
#   CORS_ALLOWED_ORIGINS="https://app.example.com,https://admin.example.com"
# Use "*" only if the API is genuinely meant to be open to any origin.
_DEFAULT_CORS_ORIGINS = ",".join([
    "http://localhost",
    "http://localhost:3000",
    "http://localhost:5173",
    "http://localhost:8000",
    "http://127.0.0.1",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:8000",
])
CORS_ALLOWED_ORIGINS: list[str] = [
    o.strip() for o in os.getenv("CORS_ALLOWED_ORIGINS", _DEFAULT_CORS_ORIGINS).split(",")
    if o.strip()
]

# --- Rate limiting (per-IP, Redis-backed via slowapi) ------------------------
# Format follows limits.parse() — "<count>/<period>" where period is
# second|minute|hour|day. Multiple limits separated by ";" — e.g.
# "10/minute;100/hour" applies BOTH (the stricter wins).
#
# Defaults sized for a Mac mini M4 with one Ollama worker, treating rate
# limits as anti-DoS rather than strict shaping (real users won't hit them):
#   /generate-data-json — each job pins Ollama for 30-90s, so 30/min/IP
#                        already exceeds what the queue can chew through.
#   /fill-form          — sync, ~1-3s per call. 120/min handles bulk fills.
#   /to-acroform        — same.
# Tighten in production via env if you have a stricter quota target.
RATE_LIMIT_GENERATE: str = os.getenv("RATE_LIMIT_GENERATE", "30/minute")
RATE_LIMIT_FILL_FORM: str = os.getenv("RATE_LIMIT_FILL_FORM", "120/minute")
RATE_LIMIT_TO_ACROFORM: str = os.getenv("RATE_LIMIT_TO_ACROFORM", "120/minute")
# "1" enables; anything else disables (useful when running tests that don't
# want to share a Redis-backed counter, or for local CLI workflows).
RATE_LIMIT_ENABLED: bool = os.getenv("RATE_LIMIT_ENABLED", "1") == "1"

# --- Webhook delivery -------------------------------------------------------
# Optional shared secret. When set, every webhook POST carries
# X-Form-Pipeline-Signature: sha256=<hmac-sha256(secret, body)> so receivers
# can verify the payload originated from this API.
WEBHOOK_SECRET: str | None = os.getenv("WEBHOOK_SECRET") or None
# Per-attempt HTTP timeout when POSTing to a caller's webhook URL. arq retries
# the delivery up to 4 times with exponential backoff (~0s, 2s, 4s, 8s), so a
# hung receiver burns ~10s × 4 = ~40s of worker time in the worst case.
WEBHOOK_TIMEOUT: float = float(os.getenv("WEBHOOK_TIMEOUT", "10"))

# SSRF guard: when True (production default), reject webhook_url values
# whose resolved host is a loopback / link-local / private / multicast /
# reserved IP. Set to "0" in self-hosted dev where the webhook receiver
# legitimately sits on a private network (e.g. localhost during testing).
WEBHOOK_BLOCK_PRIVATE: bool = os.getenv("WEBHOOK_BLOCK_PRIVATE", "1") == "1"

# --- Database (SQLite, async) ------------------------------------------------
# DB file lives inside JOBS_DIR so it shares the mounted volume in Docker.
# Override with DATABASE_URL=sqlite+aiosqlite:///path/to/file for a different
# location or a non-SQLite backend (Postgres would also need asyncpg installed).
DATABASE_URL: str = os.getenv(
    "DATABASE_URL",
    f"sqlite+aiosqlite:///{(JOBS_DIR / '_app.db').as_posix()}",
)

# --- Auth / sessions / API keys ---------------------------------------------
# Session cookie lifetime. 30 days matches typical "stay signed in" UX.
SESSION_LIFETIME_DAYS: int = int(os.getenv("SESSION_LIFETIME_DAYS", "30"))

# Set the session cookie's Secure flag. Must be True behind HTTPS in prod;
# keep False for local-dev HTTP, or browsers reject the cookie silently.
SESSION_COOKIE_SECURE: bool = os.getenv("SESSION_COOKIE_SECURE", "0") == "1"

# RSA private key (PEM) for decrypting client-encrypted login/signup passwords.
AUTH_RSA_PRIVATE_KEY_PEM: str = os.getenv("AUTH_RSA_PRIVATE_KEY_PEM", "").strip()

# Prefix tag baked into freshly-minted API keys: "sk_<env>_<32 hex>".
# Use "live" in production, "test" in staging/dev so leaked keys are
# obvious in logs.
API_KEY_ENV: str = os.getenv("API_KEY_ENV", "test")

# When True, the existing job/fill endpoints require a current user (Phase 2
# behavior). Keep False until you've wired team scoping into job_store —
# turning this on without scoping makes every authed user see every job.
AUTH_REQUIRED: bool = os.getenv("AUTH_REQUIRED", "0") == "1"

# --- Billing ---------------------------------------------------------------
# Public-facing base URL for checkout return / cancel links. Override per
# environment. Trailing slash is stripped.
APP_BASE_URL: str = os.getenv("APP_BASE_URL", "http://localhost:3000").rstrip("/")

# PayOS (Vietnam). All three are required to actually create a payment link;
# unset → the /billing/checkout endpoint refuses VN orders with 503.
PAYOS_CLIENT_ID: str | None = os.getenv("PAYOS_CLIENT_ID") or None
PAYOS_API_KEY: str | None = os.getenv("PAYOS_API_KEY") or None
PAYOS_CHECKSUM_KEY: str | None = os.getenv("PAYOS_CHECKSUM_KEY") or None
PAYOS_API_BASE: str = os.getenv("PAYOS_API_BASE", "https://api-merchant.payos.vn").rstrip("/")

# Payhip (international). API key is required to record sales; the
# webhook secret verifies signed callbacks.
PAYHIP_API_KEY: str | None = os.getenv("PAYHIP_API_KEY") or None
PAYHIP_WEBHOOK_SECRET: str | None = os.getenv("PAYHIP_WEBHOOK_SECRET") or None
PAYHIP_PRODUCT_LINK_PRO: str | None = os.getenv("PAYHIP_PRODUCT_LINK_PRO") or None
PAYHIP_PRODUCT_LINK_SCALE: str | None = os.getenv("PAYHIP_PRODUCT_LINK_SCALE") or None
