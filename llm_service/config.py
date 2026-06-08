"""Runtime configuration for the LLM orchestration service.

Read from environment variables. Defaults assume the service runs natively on
the host alongside Ollama (also on the host) and that the main app reaches it
via `host.docker.internal:11500` from inside docker-compose.
"""

from __future__ import annotations

import os


# --- Ollama ---------------------------------------------------------------- #
OLLAMA_URL: str = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "qwen3:8b")
OLLAMA_NUM_PARALLEL: int = int(os.getenv("OLLAMA_NUM_PARALLEL", "3"))
OLLAMA_KEEP_ALIVE: str = os.getenv("OLLAMA_KEEP_ALIVE", "30m")

# Per-request HTTP timeout when talking to Ollama (seconds).
OLLAMA_TIMEOUT: float = float(os.getenv("OLLAMA_TIMEOUT", "300"))

# Context window passed to Ollama. 16384 fits a ~25k-char questionnaire +
# 2-3 ~8k-char references + JSON output. Bumping to 32k+ on Apple Silicon
# adds ~260s of KV-cache allocation per call — don't.
OLLAMA_NUM_CTX: int = int(os.getenv("OLLAMA_NUM_CTX", "16384"))

# Sampling temperature. Low for deterministic structured output.
OLLAMA_TEMPERATURE: float = float(os.getenv("OLLAMA_TEMPERATURE", "0.1"))

# Per-reference character cap before truncation in the prompt.
# Rough rule: 1 token ≈ 3.5 chars for English text.
MAX_CHARS_PER_DOC: int = int(os.getenv("MAX_CHARS_PER_DOC", "8000"))

# Separate cap for the questionnaire — it defines item count, so truncating
# it loses entire questions while truncating references only weakens
# individual answers.
MAX_CHARS_PER_QUESTIONNAIRE: int = int(
    os.getenv("MAX_CHARS_PER_QUESTIONNAIRE", "25000")
)


# --- HTTP server ----------------------------------------------------------- #
LLM_SERVICE_HOST: str = os.getenv("LLM_SERVICE_HOST", "0.0.0.0")
LLM_SERVICE_PORT: int = int(os.getenv("LLM_SERVICE_PORT", "11500"))
