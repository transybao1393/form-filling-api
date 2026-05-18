"""Shared slowapi limiter instance.

Lives in its own module so route files outside api/main.py (auth router,
api_keys router, …) can register limits without importing main.py and
causing a circular dependency.
"""

from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

from . import config


_storage_uri = (
    f"redis://{config.REDIS_HOST}:{config.REDIS_PORT}"
    f"/{config.REDIS_DATABASE + 1}"
) if config.RATE_LIMIT_ENABLED else "memory://"


limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=_storage_uri,
    default_limits=[],
    enabled=config.RATE_LIMIT_ENABLED,
    headers_enabled=True,
)
