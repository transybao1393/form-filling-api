"""WebSocket authentication from upgrade headers."""

from __future__ import annotations

from typing import Any

from api import auth_utils, config, models
from api.db import get_sessionmaker


def _header_value(headers: list[tuple[bytes, bytes]], name: str) -> str | None:
    target = name.lower().encode()
    for key, value in headers:
        if key.lower() == target:
            return value.decode("latin-1")
    return None


def _parse_cookie(cookie_header: str | None, name: str) -> str | None:
    if not cookie_header:
        return None
    prefix = f"{name}="
    for part in cookie_header.split(";"):
        part = part.strip()
        if part.startswith(prefix):
            return part[len(prefix):]
    return None


def get_origin(scope: dict[str, Any]) -> str | None:
    return _header_value(scope.get("headers") or [], "origin")


async def authenticate_websocket(scope: dict[str, Any]) -> models.User | None:
    headers = scope.get("headers") or []
    cookie_header = _header_value(headers, "cookie")
    session_id = _parse_cookie(cookie_header, auth_utils.SESSION_COOKIE_NAME)
    authorization = _header_value(headers, "authorization")

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as db:
        user = await auth_utils._user_from_session(db, session_id)
        if user is None:
            user = await auth_utils._user_from_api_key(db, authorization)
        return user


def origin_allowed(scope: dict[str, Any]) -> bool:
    origin = _header_value(scope.get("headers") or [], "origin")
    if not origin:
        return True
    allowed = set(config.CORS_ALLOWED_ORIGINS)
    return "*" in allowed or origin in allowed
