"""Team + members + invites endpoints.

Role hierarchy:
- Owner: full control. Cannot be removed if they're the last Owner.
- Admin: manage members, invites, API keys, billing.
- Member: read-only on members; full use of jobs/templates/documents.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Path, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import auth_utils, config, models
from ..auth_utils import SESSION_COOKIE_NAME
from ..db import get_db


router = APIRouter(tags=["team"])


_ROLES = ("Owner", "Admin", "Member")
_INVITE_TTL_DAYS = 14


class MemberResponse(BaseModel):
    id: int
    email: str
    name: str | None
    role: str
    created_at: datetime


class InviteCreateRequest(BaseModel):
    email: EmailStr
    role: str = Field(default="Member")


class InviteResponse(BaseModel):
    id: int
    email: str
    role: str
    token: str
    accept_url: str
    invited_by_user_id: int | None
    created_at: datetime
    expires_at: datetime
    accepted_at: datetime | None


class MemberPatchRequest(BaseModel):
    role: str = Field(..., description="Owner | Admin | Member")


class TeamResponse(BaseModel):
    id: int
    name: str
    created_at: datetime
    member_count: int


class AcceptInviteRequest(BaseModel):
    token: str = Field(..., min_length=10, max_length=128)
    password: str = Field(..., min_length=8, max_length=128)
    name: str | None = Field(default=None, max_length=120)


def _require_role(user: models.User, *allowed: str) -> None:
    if user.role not in allowed:
        raise HTTPException(
            status_code=403,
            detail=f"requires role in {allowed}; you are {user.role}",
        )


def _check_role_value(role: str) -> None:
    if role not in _ROLES:
        raise HTTPException(
            status_code=400,
            detail=f"role must be one of {_ROLES}, got {role!r}",
        )


@router.get(
    "/team",
    response_model=TeamResponse,
    summary="Current team info",
)
async def get_team(
    user: models.User = Depends(auth_utils.get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TeamResponse:
    team = await db.get(models.Team, user.team_id)
    if team is None:
        raise HTTPException(status_code=404, detail="team not found")
    count_result = await db.execute(
        select(models.User).where(models.User.team_id == user.team_id)
    )
    return TeamResponse(
        id=team.id,
        name=team.name,
        created_at=team.created_at,
        member_count=len(count_result.scalars().all()),
    )


@router.get(
    "/team/members",
    response_model=list[MemberResponse],
    summary="List members of the current team",
)
async def list_members(
    user: models.User = Depends(auth_utils.get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[MemberResponse]:
    result = await db.execute(
        select(models.User)
        .where(models.User.team_id == user.team_id)
        .order_by(models.User.created_at.asc())
    )
    return [
        MemberResponse(
            id=u.id, email=u.email, name=u.name,
            role=u.role, created_at=u.created_at,
        )
        for u in result.scalars()
    ]


@router.post(
    "/team/invite",
    response_model=InviteResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an invite token (Owner/Admin only)",
)
async def create_invite(
    body: InviteCreateRequest,
    user: models.User = Depends(auth_utils.get_current_user),
    db: AsyncSession = Depends(get_db),
) -> InviteResponse:
    _require_role(user, "Owner", "Admin")
    _check_role_value(body.role)
    if body.role == "Owner":
        _require_role(user, "Owner")

    # Block invites to emails already in this team.
    existing = await db.execute(
        select(models.User).where(
            models.User.email == body.email,
            models.User.team_id == user.team_id,
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="user already in team")

    now = datetime.now(timezone.utc)
    token = secrets.token_urlsafe(32)
    invite = models.TeamInvite(
        team_id=user.team_id,
        email=body.email,
        role=body.role,
        token=token,
        invited_by_user_id=user.id,
        created_at=now,
        expires_at=now + timedelta(days=_INVITE_TTL_DAYS),
    )
    db.add(invite)
    await db.commit()
    await db.refresh(invite)
    return InviteResponse(
        id=invite.id,
        email=invite.email,
        role=invite.role,
        token=invite.token,
        accept_url=f"/auth/accept-invite?token={invite.token}",
        invited_by_user_id=invite.invited_by_user_id,
        created_at=invite.created_at,
        expires_at=invite.expires_at,
        accepted_at=invite.accepted_at,
    )


@router.get(
    "/team/invites",
    response_model=list[InviteResponse],
    summary="List pending invites (Owner/Admin only)",
)
async def list_invites(
    user: models.User = Depends(auth_utils.get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[InviteResponse]:
    _require_role(user, "Owner", "Admin")
    result = await db.execute(
        select(models.TeamInvite)
        .where(
            models.TeamInvite.team_id == user.team_id,
            models.TeamInvite.accepted_at.is_(None),
        )
        .order_by(models.TeamInvite.created_at.desc())
    )
    return [
        InviteResponse(
            id=inv.id,
            email=inv.email,
            role=inv.role,
            token=inv.token,
            accept_url=f"/auth/accept-invite?token={inv.token}",
            invited_by_user_id=inv.invited_by_user_id,
            created_at=inv.created_at,
            expires_at=inv.expires_at,
            accepted_at=inv.accepted_at,
        )
        for inv in result.scalars()
    ]


@router.delete(
    "/team/invites/{invite_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke a pending invite",
)
async def revoke_invite(
    invite_id: int = Path(..., ge=1),
    user: models.User = Depends(auth_utils.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_role(user, "Owner", "Admin")
    result = await db.execute(
        select(models.TeamInvite).where(
            models.TeamInvite.id == invite_id,
            models.TeamInvite.team_id == user.team_id,
            models.TeamInvite.accepted_at.is_(None),
        )
    )
    inv = result.scalar_one_or_none()
    if inv is None:
        raise HTTPException(status_code=404, detail="invite not found")
    await db.delete(inv)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch(
    "/team/members/{user_id}",
    response_model=MemberResponse,
    summary="Change a member's role (Owner only)",
)
async def update_member_role(
    body: MemberPatchRequest,
    user_id: int = Path(..., ge=1),
    user: models.User = Depends(auth_utils.get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MemberResponse:
    _require_role(user, "Owner")
    _check_role_value(body.role)
    target = await db.get(models.User, user_id)
    if target is None or target.team_id != user.team_id:
        raise HTTPException(status_code=404, detail="member not found")
    if target.id == user.id and body.role != "Owner":
        # Refuse to demote yourself if you're the last Owner.
        owners = await db.execute(
            select(models.User).where(
                models.User.team_id == user.team_id,
                models.User.role == "Owner",
            )
        )
        if len(owners.scalars().all()) <= 1:
            raise HTTPException(
                status_code=400,
                detail="cannot demote the last Owner",
            )
    target.role = body.role
    await db.commit()
    await db.refresh(target)
    return MemberResponse(
        id=target.id, email=target.email, name=target.name,
        role=target.role, created_at=target.created_at,
    )


@router.delete(
    "/team/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a member from the team (Owner/Admin)",
)
async def remove_member(
    user_id: int = Path(..., ge=1),
    user: models.User = Depends(auth_utils.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_role(user, "Owner", "Admin")
    target = await db.get(models.User, user_id)
    if target is None or target.team_id != user.team_id:
        raise HTTPException(status_code=404, detail="member not found")
    if target.role == "Owner":
        owners = await db.execute(
            select(models.User).where(
                models.User.team_id == user.team_id,
                models.User.role == "Owner",
            )
        )
        if len(owners.scalars().all()) <= 1:
            raise HTTPException(
                status_code=400,
                detail="cannot remove the last Owner",
            )

    # ON DELETE CASCADE on user_sessions.user_id + api_keys.user_id (see
    # models.py) takes care of dropping active sessions and revoking keys
    # at the DB level — a force-logout falls out automatically.
    await db.delete(target)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# This endpoint logically belongs with /auth/* but lives here to keep the
# invite-acceptance code colocated with invite creation. It's mounted at the
# top level via the router include.
@router.post(
    "/auth/accept-invite",
    summary="Create an account using an invite token",
    status_code=status.HTTP_201_CREATED,
)
async def accept_invite(
    body: AcceptInviteRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(models.TeamInvite).where(models.TeamInvite.token == body.token)
    )
    inv = result.scalar_one_or_none()
    if inv is None:
        raise HTTPException(status_code=404, detail="invite not found")
    if inv.accepted_at is not None:
        raise HTTPException(status_code=409, detail="invite already accepted")
    inv_expires = inv.expires_at
    if inv_expires is not None and inv_expires.tzinfo is None:
        inv_expires = inv_expires.replace(tzinfo=timezone.utc)
    if inv_expires is None or inv_expires < datetime.now(timezone.utc):
        raise HTTPException(status_code=410, detail="invite expired")

    existing = await db.execute(
        select(models.User).where(models.User.email == inv.email)
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="email already has an account")

    now = datetime.now(timezone.utc)
    user = models.User(
        email=inv.email,
        password_hash=auth_utils.hash_password(body.password),
        name=body.name,
        team_id=inv.team_id,
        role=inv.role,
        created_at=now,
    )
    db.add(user)
    await db.flush()
    inv.accepted_at = now
    inv.accepted_by_user_id = user.id
    await db.commit()
    await db.refresh(user)

    # Start a session immediately so the invitee lands in the dashboard
    # without a second password prompt.
    sess = await auth_utils.create_session(db, user.id)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=sess.id,
        max_age=config.SESSION_LIFETIME_DAYS * 86400,
        httponly=True,
        samesite="lax",
        secure=config.SESSION_COOKIE_SECURE,
        path="/",
    )
    return {
        "id": user.id,
        "email": user.email,
        "team_id": user.team_id,
        "role": user.role,
        "created_at": user.created_at,
    }
