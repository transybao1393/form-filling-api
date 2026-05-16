"""API key CRUD: list, mint (returns secret once), revoke."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Path, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import auth_utils, config, models
from ..db import get_db


router = APIRouter(prefix="/api-keys", tags=["api-keys"])


class ApiKeyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)


class ApiKeyItem(BaseModel):
    id: int
    name: str
    prefix: str
    environment: str
    created_at: datetime
    last_used_at: datetime | None
    request_count: int


class ApiKeyCreated(ApiKeyItem):
    secret: str = Field(
        ...,
        description="Full API key. Shown ONCE on creation; not retrievable later.",
    )


@router.get(
    "",
    response_model=list[ApiKeyItem],
    summary="List your API keys (no secrets)",
)
async def list_keys(
    user: models.User = Depends(auth_utils.get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ApiKeyItem]:
    result = await db.execute(
        select(models.ApiKey)
        .where(
            models.ApiKey.user_id == user.id,
            models.ApiKey.revoked_at.is_(None),
        )
        .order_by(models.ApiKey.created_at.desc())
    )
    return [
        ApiKeyItem(
            id=k.id,
            name=k.name,
            prefix=k.prefix,
            environment=k.environment,
            created_at=k.created_at,
            last_used_at=k.last_used_at,
            request_count=k.request_count,
        )
        for k in result.scalars()
    ]


@router.post(
    "",
    response_model=ApiKeyCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Mint a new API key (secret returned once)",
)
async def create_key(
    body: ApiKeyCreate,
    user: models.User = Depends(auth_utils.get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ApiKeyCreated:
    full, h, prefix = auth_utils.generate_api_key()
    now = datetime.now(timezone.utc)
    key = models.ApiKey(
        user_id=user.id,
        team_id=user.team_id,
        name=body.name,
        hash=h,
        prefix=prefix,
        environment=config.API_KEY_ENV,
        created_at=now,
    )
    db.add(key)
    await db.commit()
    await db.refresh(key)
    return ApiKeyCreated(
        id=key.id,
        name=key.name,
        prefix=key.prefix,
        environment=key.environment,
        created_at=key.created_at,
        last_used_at=key.last_used_at,
        request_count=key.request_count,
        secret=full,
    )


@router.delete(
    "/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke (soft-delete) an API key",
)
async def revoke_key(
    key_id: int = Path(..., ge=1),
    user: models.User = Depends(auth_utils.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(models.ApiKey).where(
            models.ApiKey.id == key_id,
            models.ApiKey.user_id == user.id,
        )
    )
    key = result.scalar_one_or_none()
    if key is None or key.revoked_at is not None:
        raise HTTPException(status_code=404, detail="Key not found")
    key.revoked_at = datetime.now(timezone.utc)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
