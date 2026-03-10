from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class OperationAcceptedResponse(BaseModel):
    operation_id: str
    status: str = "queued"


class OperationStatusResponse(BaseModel):
    operation_id: str
    user_id: str
    operation_type: str
    status: str
    step: str | None = None
    requested_image_tag: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    old_generation: int | None = None
    new_generation: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    created_at: str | None = None
    metadata: dict[str, Any] | None = None


class RuntimeStatusResponse(BaseModel):
    user_id: str
    runtime_status: str
    current_generation: int | None = None
    active_container_name: str | None = None
    active_image_tag: str | None = None
    previous_generation: int | None = None
    previous_image_tag: str | None = None
    health_status: str | None = None
    last_health_checked_at: str | None = None
    last_operation: dict[str, Any] | None = None

