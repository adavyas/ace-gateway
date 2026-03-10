from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class NewUserRequest(BaseModel):
    user_id: UUID
    image_tag: str | None = None
    env_overrides: dict[str, str] = Field(default_factory=dict)
    start_immediately: bool = True


class DeployRequest(BaseModel):
    image_tag: str
    env_overrides: dict[str, str] = Field(default_factory=dict)
    keep_previous_warm_seconds: int | None = None


class RollbackRequest(BaseModel):
    reason: str | None = None


class RestartRequest(BaseModel):
    mode: Literal["safe", "force"] = "safe"

