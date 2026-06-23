"""WebSocket service configuration — reads same env vars as api.config."""

from __future__ import annotations

from api import config

REDIS_HOST = config.REDIS_HOST
REDIS_PORT = config.REDIS_PORT
REDIS_DATABASE = config.REDIS_DATABASE
JOBS_DIR = config.JOBS_DIR
AUTH_REQUIRED = config.AUTH_REQUIRED
CORS_ALLOWED_ORIGINS = config.CORS_ALLOWED_ORIGINS

HEARTBEAT_SEC = 30
PONG_TIMEOUT_SEC = 10
MAX_CHANNELS_PER_CONNECTION = 20
PROTOCOL_VERSION = 1
