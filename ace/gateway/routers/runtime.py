from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth.deps import CurrentUser, get_current_user
from ..runtime_manager import (
    RuntimeEnsureResult,
    RuntimeOptions,
    RuntimeProvisioningError,
    ensure_user_runtime,
)

router = APIRouter(tags=["runtime"])


class RuntimeResources(BaseModel):
    cpus: Optional[float] = Field(default=None, gt=0)
    memory: Optional[str] = None


class NewUserRequest(BaseModel):
    image_tag: Optional[str] = None
    resources: Optional[RuntimeResources] = None
    env_overrides: Optional[dict[str, str]] = None


class NewUserResponse(BaseModel):
    user_id: str
    container_name: str
    volume_name: str
    upstream_url: str
    status: str
    image_ref: str
    cached: bool = False


def _to_response(result: RuntimeEnsureResult) -> NewUserResponse:
    return NewUserResponse(
        user_id=result.user_id,
        container_name=result.container_name,
        volume_name=result.volume_name,
        upstream_url=result.upstream_url,
        status=result.status,
        image_ref=result.image_ref,
        cached=result.cached,
    )


@router.post("/newuser", response_model=NewUserResponse)
async def new_user(
    payload: Optional[NewUserRequest] = Body(default=None),
    current_user: CurrentUser = Depends(get_current_user),
) -> NewUserResponse:
    body = payload or NewUserRequest()
    resources = body.resources
    options = RuntimeOptions(
        image_tag=body.image_tag,
        cpus=(resources.cpus if resources else None),
        memory=(resources.memory if resources else None),
        env_overrides=body.env_overrides,
        allow_cached=False,
        wait_for_lock=True,
    )
    try:
        result = await ensure_user_runtime(current_user.user_id, options=options)
    except RuntimeProvisioningError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return _to_response(result)
