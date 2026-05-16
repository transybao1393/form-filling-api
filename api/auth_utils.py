"""Auth utilities: password hashing, session + API key management, deps.

Two ways to authenticate against any endpoint that depends on
`get_current_user`:

1. Browser/dashboard: HTTP-only session cookie (`vellum_session`) set by
   POST /auth/login or POST /auth/signup. SameSite=Lax blocks cross-site
   POSTs from carrying the cookie (CSRF mitigation).
2. Programmatic: `Authorization: Bearer sk_live_...` (matches the curl
   examples documented on the dashboard's /api page).

Cookie wins if both are present.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from fastapi import Cookie, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import config, models
from .db import get_db


SESSION_COOKIE_NAME = "vellum_session"
API_KEY_PREFIX_LEN = 12  # "sk_live_" (8) + 4 visible body chars

_hasher = PasswordHasher()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: datetime | None) -> datetime | None:
    """SQLite DateTime columns lose tzinfo on read — treat naive values as
    UTC so comparisons against `_now()` (timezone-aware) don't raise
    `TypeError: can't compare offset-naive and offset-aware datetimes`.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# --- Passwords --------------------------------------------------------------

def hash_password(plain: str) -> str:
    return _hasher.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    # argon2-cffi raises a small family of subclasses for bad input
    # (VerifyMismatchError, InvalidHash, …). Treat all of them as "no" so
    # callers get a clean bool. We don't differentiate "wrong password"
    # from "corrupt hash" to keep the timing/error signal uniform.
    try:
        _hasher.verify(hashed, plain)
        return True
    except Exception:
        return False


# --- Sessions ---------------------------------------------------------------

def _gen_session_id() -> str:
    return secrets.token_urlsafe(32)


async def create_session(db: AsyncSession, user_id: int) -> models.UserSession:
    sess = models.UserSession(
        id=_gen_session_id(),
        user_id=user_id,
        created_at=_now(),
        expires_at=_now() + timedelta(days=config.SESSION_LIFETIME_DAYS),
    )
    db.add(sess)
    await db.commit()
    await db.refresh(sess)
    return sess


async def delete_session(db: AsyncSession, session_id: str) -> None:
    await db.execute(
        models.UserSession.__table__.delete().where(
            models.UserSession.id == session_id
        )
    )
    await db.commit()


# --- API keys ---------------------------------------------------------------

def _key_prefix() -> str:
    return f"sk_{config.API_KEY_ENV}_"


def generate_api_key() -> tuple[str, str, str]:
    """Return (full_key, sha256_hex, visible_prefix). Full key is shown
    to the user once at creation time; only the hash is persisted."""
    body = secrets.token_hex(16)  # 32 hex chars
    full = _key_prefix() + body
    h = hashlib.sha256(full.encode()).hexdigest()
    prefix = full[:API_KEY_PREFIX_LEN]
    return full, h, prefix


def hash_api_key(full: str) -> str:
    return hashlib.sha256(full.encode()).hexdigest()


# --- FastAPI dependencies ---------------------------------------------------

async def _user_from_session(
    db: AsyncSession, session_id: str | None,
) -> models.User | None:
    if not session_id:
        return None
    result = await db.execute(
        select(models.UserSession).where(models.UserSession.id == session_id)
    )
    sess = result.scalar_one_or_none()
    if sess is None:
        return None
    expires = _as_utc(sess.expires_at)
    if expires is None or expires < _now():
        return None
    return await db.get(models.User, sess.user_id)


async def _user_from_api_key(
    db: AsyncSession, authorization: str | None,
) -> models.User | None:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    full = authorization.split(" ", 1)[1].strip()
    if not full:
        return None
    result = await db.execute(
        select(models.ApiKey).where(
            models.ApiKey.hash == hash_api_key(full),
            models.ApiKey.revoked_at.is_(None),
        )
    )
    key = result.scalar_one_or_none()
    if key is None:
        return None
    user = await db.get(models.User, key.user_id)
    if user is not None:
        key.last_used_at = _now()
        key.request_count = key.request_count + 1
        await db.commit()
    return user


async def get_current_user(
    db: AsyncSession = Depends(get_db),
    session_cookie: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    authorization: str | None = Header(default=None),
) -> models.User:
    user = await _user_from_session(db, session_cookie)
    if user is None:
        user = await _user_from_api_key(db, authorization)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


async def get_current_user_optional(
    db: AsyncSession = Depends(get_db),
    session_cookie: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    authorization: str | None = Header(default=None),
) -> models.User | None:
    user = await _user_from_session(db, session_cookie)
    if user is None:
        user = await _user_from_api_key(db, authorization)
    return user


async def auth_for_jobs(
    db: AsyncSession = Depends(get_db),
    session_cookie: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    authorization: str | None = Header(default=None),
) -> models.User | None:
    """Auth dependency for the legacy job/fill endpoints.

    Returns User if authenticated, None if not. Raises 401 only when
    config.AUTH_REQUIRED is True — so existing unauthenticated clients keep
    working until the operator opts in by flipping AUTH_REQUIRED=1.
    """
    user = await _user_from_session(db, session_cookie)
    if user is None:
        user = await _user_from_api_key(db, authorization)
    if user is None and config.AUTH_REQUIRED:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user
