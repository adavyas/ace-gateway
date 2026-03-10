from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI

from db.database import SessionLocal, init_db

from ace_net_manager.api.routes import router as manager_router
from ace_net_manager.services.deploy_service import DeployService
from ace_net_manager.services.docker_service import DockerService
from ace_net_manager.services.health_service import HealthService
from ace_net_manager.services.lock_service import LockService
from ace_net_manager.services.operation_service import OperationService
from ace_net_manager.services.provision_service import ProvisionService
from ace_net_manager.services.restart_service import RestartService
from ace_net_manager.services.rollback_service import RollbackService
from ace_net_manager.services.runtime_service import RuntimeService
from ace_net_manager.settings import ManagerSettings, get_settings
from ace_net_manager.workers.operation_runner import OperationRunner


@dataclass
class ManagerServiceBundle:
    settings: ManagerSettings
    runtime_service: RuntimeService
    operation_service: OperationService
    operation_runner: OperationRunner


def build_default_services(settings: ManagerSettings) -> ManagerServiceBundle:
    runtime_service = RuntimeService()
    docker_service = DockerService(settings)
    health_service = HealthService(settings, docker_service=docker_service)
    lock_service = LockService()

    rollback_service = RollbackService(
        settings=settings,
        runtime_service=runtime_service,
        docker_service=docker_service,
        health_service=health_service,
    )
    deploy_service = DeployService(
        settings=settings,
        runtime_service=runtime_service,
        docker_service=docker_service,
        health_service=health_service,
    )
    deploy_service.set_rollback_service(rollback_service)
    restart_service = RestartService(
        settings=settings,
        runtime_service=runtime_service,
        docker_service=docker_service,
        health_service=health_service,
        deploy_service=deploy_service,
    )
    provision_service = ProvisionService(
        settings=settings,
        runtime_service=runtime_service,
        docker_service=docker_service,
        health_service=health_service,
    )
    operation_service = OperationService(
        runtime_service=runtime_service,
        provision_service=provision_service,
        deploy_service=deploy_service,
        rollback_service=rollback_service,
        restart_service=restart_service,
    )
    operation_runner = OperationRunner(
        session_factory=SessionLocal,
        operation_service=operation_service,
        lock_service=lock_service,
        max_concurrency=settings.ace_max_global_concurrent_ops,
        operation_timeout_seconds=settings.ace_op_max_seconds,
    )
    return ManagerServiceBundle(
        settings=settings,
        runtime_service=runtime_service,
        operation_service=operation_service,
        operation_runner=operation_runner,
    )


def create_app(
    *,
    service_bundle: ManagerServiceBundle | None = None,
    init_database_on_start: bool = True,
    start_runner_on_start: bool = True,
) -> FastAPI:
    app = FastAPI(title="Ace Net Manager")
    settings = get_settings()
    bundle = service_bundle or build_default_services(settings)
    app.state.manager_services = bundle
    app.include_router(manager_router)

    @app.on_event("startup")
    async def _startup() -> None:
        if init_database_on_start:
            init_db()
        if init_database_on_start:
            db = SessionLocal()
            try:
                bundle.operation_service.fail_stale_inflight_operations(
                    db,
                    stale_minutes=max(1, int(settings.ace_stale_operation_grace_minutes)),
                )
            finally:
                db.close()
        if start_runner_on_start:
            await bundle.operation_runner.start()

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await bundle.operation_runner.shutdown()

    @app.get("/healthz")
    async def _health() -> dict[str, Any]:
        return {"status": "ok", "service": "ace-net-manager"}

    return app


def _safe_default_app() -> FastAPI:
    try:
        return create_app()
    except Exception as exc:  # pragma: no cover
        detail = str(exc)
        app = FastAPI(title="Ace Net Manager")

        @app.get("/healthz")
        async def _health() -> dict[str, Any]:
            return {"status": "error", "service": "ace-net-manager", "detail": detail}

        return app


app = _safe_default_app()
