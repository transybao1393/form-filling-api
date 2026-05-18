"""Authentication endpoints: signup, login, logout, me."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import auth_utils, config, models
from ..auth_utils import SESSION_COOKIE_NAME
from ..db import get_db
from ..rate_limit import limiter


router = APIRouter(prefix="/auth", tags=["auth"])


class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)
    name: str | None = Field(default=None, max_length=120)
    team_name: str | None = Field(default=None, max_length=120)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=1, max_length=128)


class UserResponse(BaseModel):
    id: int
    email: str
    name: str | None
    role: str
    team_id: int
    team_name: str
    created_at: datetime


async def _user_response(db: AsyncSession, user: models.User) -> UserResponse:
    team = await db.get(models.Team, user.team_id)
    return UserResponse(
        id=user.id,
        email=user.email,
        name=user.name,
        role=user.role,
        team_id=user.team_id,
        team_name=team.name if team else "",
        created_at=user.created_at,
    )


def _set_session_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_id,
        max_age=config.SESSION_LIFETIME_DAYS * 86400,
        httponly=True,
        samesite="lax",
        secure=config.SESSION_COOKIE_SECURE,
        path="/",
    )


@router.post(
    "/signup",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new account + team and start a session",
)
@limiter.limit("3/minute")
async def signup(
    request: Request,
    body: SignupRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    existing = await db.execute(
        select(models.User).where(models.User.email == body.email)
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="Email already in use")

    now = datetime.now(timezone.utc)
    team = models.Team(
        name=body.team_name or f"{body.email.split('@')[0]}'s team",
        created_at=now,
    )
    db.add(team)
    await db.flush()

    user = models.User(
        email=body.email,
        password_hash=auth_utils.hash_password(body.password),
        name=body.name,
        team_id=team.id,
        role="Owner",
        created_at=now,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    sess = await auth_utils.create_session(db, user.id)
    _set_session_cookie(response, sess.id)
    return await _user_response(db, user)


@router.post(
    "/login",
    response_model=UserResponse,
    summary="Email + password login (sets session cookie)",
)
@limiter.limit("5/minute")
async def login(
    request: Request,
    body: LoginRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    result = await db.execute(
        select(models.User).where(models.User.email == body.email)
    )
    user = result.scalar_one_or_none()
    if user is None or not auth_utils.verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    sess = await auth_utils.create_session(db, user.id)
    _set_session_cookie(response, sess.id)
    return await _user_response(db, user)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Clear the current session",
)
async def logout(
    response: Response,
    db: AsyncSession = Depends(get_db),
    session_cookie: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
):
    if session_cookie:
        await auth_utils.delete_session(db, session_cookie)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/me",
    response_model=UserResponse,
    summary="Current user info",
)
async def me(
    user: models.User = Depends(auth_utils.get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    return await _user_response(db, user)
