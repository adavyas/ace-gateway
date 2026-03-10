from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from db.database import SessionLocal

from ace_net_manager.schemas.requests import DeployRequest, NewUserRequest, RestartRequest, RollbackRequest
from ace_net_manager.schemas.responses import (
    OperationAcceptedResponse,
    OperationStatusResponse,
    RuntimeStatusResponse,
)
from ace_net_manager.security import require_internal_bearer_token
from ace_net_manager.utils.json_safe import to_jsonable


router = APIRouter(dependencies=[Depends(require_internal_bearer_token)])


def _model_dump(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return to_jsonable(value.model_dump(mode="json"))  # pydantic v2
    return to_jsonable(value.dict())  # pydantic v1


def _iso(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.isoformat()
    return str(v)


def _services(request: Request):
    bundle = getattr(request.app.state, "manager_services", None)
    if bundle is None:
        raise HTTPException(status_code=500, detail="manager services unavailable")
    return bundle


@router.post("/new-user", response_model=OperationAcceptedResponse, status_code=202)
async def new_user(payload: NewUserRequest, request: Request):
    services = _services(request)
    user_id = str(payload.user_id)
    data = _model_dump(payload)
    requested_image = data.get("image_tag") or services.settings.ace_default_image_tag
    data["image_tag"] = requested_image
    data["default_image_tag"] = services.settings.ace_default_image_tag

    db = SessionLocal()
    try:
        if services.operation_service.has_inflight_operation(db, user_id):
            raise HTTPException(status_code=409, detail="another operation is already in progress for this user")
        operation_id = services.operation_service.create_operation(
            db,
            user_id=user_id,
            operation_type="new_user",
            requested_by="admin",
            requested_image_tag=requested_image,
            metadata=data,
        )
    finally:
        db.close()

    await services.operation_runner.enqueue(operation_id)
    return OperationAcceptedResponse(operation_id=operation_id, status="queued")


@router.post("/deploy/{user_id}", response_model=OperationAcceptedResponse, status_code=202)
async def deploy_user(user_id: str, payload: DeployRequest, request: Request):
    services = _services(request)
    db = SessionLocal()
    try:
        if services.operation_service.has_inflight_operation(db, user_id):
            raise HTTPException(status_code=409, detail="another operation is already in progress for this user")
        data = _model_dump(payload)
        operation_id = services.operation_service.create_operation(
            db,
            user_id=user_id,
            operation_type="deploy",
            requested_by="admin",
            requested_image_tag=str(data["image_tag"]),
            metadata=data,
        )
    finally:
        db.close()

    await services.operation_runner.enqueue(operation_id)
    return OperationAcceptedResponse(operation_id=operation_id, status="queued")


@router.post("/rollback/{user_id}", response_model=OperationAcceptedResponse, status_code=202)
async def rollback_user(user_id: str, payload: RollbackRequest, request: Request):
    services = _services(request)
    db = SessionLocal()
    try:
        if services.operation_service.has_inflight_operation(db, user_id):
            raise HTTPException(status_code=409, detail="another operation is already in progress for this user")
        operation_id = services.operation_service.create_operation(
            db,
            user_id=user_id,
            operation_type="rollback",
            requested_by="admin",
            requested_image_tag=None,
            metadata=_model_dump(payload),
        )
    finally:
        db.close()

    await services.operation_runner.enqueue(operation_id)
    return OperationAcceptedResponse(operation_id=operation_id, status="queued")


@router.post("/restart/{user_id}", response_model=OperationAcceptedResponse, status_code=202)
async def restart_user(user_id: str, payload: RestartRequest, request: Request):
    services = _services(request)
    db = SessionLocal()
    try:
        if services.operation_service.has_inflight_operation(db, user_id):
            raise HTTPException(status_code=409, detail="another operation is already in progress for this user")
        operation_id = services.operation_service.create_operation(
            db,
            user_id=user_id,
            operation_type="restart",
            requested_by="admin",
            requested_image_tag=None,
            metadata=_model_dump(payload),
        )
    finally:
        db.close()

    await services.operation_runner.enqueue(operation_id)
    return OperationAcceptedResponse(operation_id=operation_id, status="queued")


@router.get("/status/{user_id}", response_model=RuntimeStatusResponse)
async def status_user(user_id: str, request: Request):
    services = _services(request)
    db = SessionLocal()
    try:
        runtime = services.runtime_service.get_runtime(db, user_id)
        if runtime is None:
            raise HTTPException(status_code=404, detail="runtime not found")

        last_op = None
        op_id = runtime.get("last_operation_id")
        if op_id:
            op = services.operation_service.get_operation(db, str(op_id))
            if op:
                last_op = {
                    "operation_id": str(op.get("operation_id")),
                    "type": op.get("operation_type"),
                    "status": op.get("status"),
                }
        return RuntimeStatusResponse(
            user_id=str(runtime.get("user_id")),
            runtime_status=str(runtime.get("status")),
            current_generation=runtime.get("current_generation"),
            active_container_name=runtime.get("active_container_name"),
            active_image_tag=runtime.get("active_image_tag"),
            previous_generation=runtime.get("previous_generation"),
            previous_image_tag=runtime.get("previous_image_tag"),
            health_status=runtime.get("last_health_status"),
            last_health_checked_at=_iso(runtime.get("last_health_checked_at")),
            last_operation=last_op,
        )
    finally:
        db.close()


@router.get("/operations/{operation_id}", response_model=OperationStatusResponse)
async def operation_status(operation_id: str, request: Request):
    services = _services(request)
    db = SessionLocal()
    try:
        row = services.operation_service.get_operation(db, operation_id)
        if row is None:
            raise HTTPException(status_code=404, detail="operation not found")
        op = services.operation_service.serialize_operation(row)
        return OperationStatusResponse(
            operation_id=str(op.get("operation_id")),
            user_id=str(op.get("user_id")),
            operation_type=str(op.get("operation_type")),
            status=str(op.get("status")),
            step=op.get("step"),
            requested_image_tag=op.get("requested_image_tag"),
            error_code=op.get("error_code"),
            error_message=op.get("error_message"),
            old_generation=op.get("old_generation"),
            new_generation=op.get("new_generation"),
            started_at=op.get("started_at"),
            finished_at=op.get("finished_at"),
            created_at=op.get("created_at"),
            metadata=op.get("metadata"),
        )
    finally:
        db.close()
