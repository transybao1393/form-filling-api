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
