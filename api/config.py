"""Runtime configuration read from environment variables."""

from __future__ import annotations

import os
from pathlib import Path


OLLAMA_URL: str = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "qwen3:8b")

# Per-request HTTP timeout when talking to Ollama (seconds).
OLLAMA_TIMEOUT: float = float(os.getenv("OLLAMA_TIMEOUT", "300"))

# Context window passed to Ollama. 16384 tokens fits a ~25k-char questionnaire
# + a couple of ~8k-char references + the JSON response. Going to 32k+ on
# Apple Silicon adds ~260s of KV-cache allocation per call — don't.
OLLAMA_NUM_CTX: int = int(os.getenv("OLLAMA_NUM_CTX", "16384"))

# Sampling temperature. Low for deterministic structured output.
OLLAMA_TEMPERATURE: float = float(os.getenv("OLLAMA_TEMPERATURE", "0.1"))

# Per-reference character cap before truncation in the prompt.
# Rough rule: 1 token ≈ 3.5 chars for English text.
MAX_CHARS_PER_DOC: int = int(os.getenv("MAX_CHARS_PER_DOC", "8000"))

# Separate cap for the questionnaire — it defines item count, so truncating
# it loses entire questions while truncating references only weakens
# individual answers. Sized to fit ~7k tokens within OLLAMA_NUM_CTX=16384,
# leaving room for system prompt + 2-3 references + JSON output.
MAX_CHARS_PER_QUESTIONNAIRE: int = int(
    os.getenv("MAX_CHARS_PER_QUESTIONNAIRE", "25000")
)

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
