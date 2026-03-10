from __future__ import annotations

import asyncio
from typing import Callable

from ace_net_manager.utils.errors import OperationError, OperationRolledBack


class OperationRunner:
    def __init__(
        self,
        *,
        session_factory: Callable,
        operation_service,
        lock_service,
        max_concurrency: int = 3,
        operation_timeout_seconds: int = 600,
    ) -> None:
        self._session_factory = session_factory
        self._operation_service = operation_service
        self._lock_service = lock_service
        self._max_concurrency = max(1, int(max_concurrency))
        self._operation_timeout_seconds = max(1, int(operation_timeout_seconds))
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        for idx in range(self._max_concurrency):
            self._workers.append(asyncio.create_task(self._worker_loop(idx)))

    async def shutdown(self) -> None:
        if not self._started:
            return
        self._started = False
        for task in self._workers:
            task.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    async def enqueue(self, operation_id: str) -> None:
        await self._queue.put(str(operation_id))

    async def _worker_loop(self, _worker_index: int) -> None:
        while True:
            operation_id = await self._queue.get()
            try:
                await self._process_one(operation_id)
            finally:
                self._queue.task_done()

    async def _process_one(self, operation_id: str) -> None:
        db = self._session_factory()
        acquired = False
        user_id = None
        operation: dict | None = None
        try:
            operation = self._operation_service.get_operation(db, operation_id)
            if not operation:
                return
            if str(operation.get("status")) not in {"queued", "running"}:
                return
            user_id = str(operation["user_id"])
            self._operation_service.mark_running(db, operation_id, step="running")

            acquired = self._lock_service.try_acquire(db, user_id)
            if not acquired:
                self._operation_service.mark_failed(
                    db,
                    operation_id,
                    error_code="conflict",
                    error_message="another control operation is already running for this user",
                )
                return

            result = await asyncio.wait_for(
                self._operation_service.execute(db, operation),
                timeout=self._operation_timeout_seconds,
            )
            self._operation_service.mark_succeeded(
                db,
                operation_id,
                old_generation=result.get("old_generation"),
                new_generation=result.get("new_generation"),
            )
        except asyncio.TimeoutError:
            db.rollback()
            cleanup_note = "cleanup_not_attempted"
            if operation is not None:
                try:
                    cleanup_note = await self._operation_service.handle_timeout_cleanup(db, operation)
                except Exception as cleanup_exc:  # pragma: no cover
                    cleanup_note = f"cleanup_failed:{cleanup_exc}"
            self._operation_service.mark_failed(
                db,
                operation_id,
                error_code="operation_timeout",
                error_message=(
                    f"operation exceeded {self._operation_timeout_seconds}s deadline; "
                    f"cleanup={cleanup_note}"
                ),
            )
        except OperationRolledBack as exc:
            db.rollback()
            self._operation_service.mark_rolled_back(db, operation_id, str(exc))
        except OperationError as exc:
            db.rollback()
            self._operation_service.mark_failed(
                db,
                operation_id,
                error_code=exc.code,
                error_message=exc.message,
            )
        except Exception as exc:  # pragma: no cover
            db.rollback()
            self._operation_service.mark_failed(
                db,
                operation_id,
                error_code="unexpected_error",
                error_message=str(exc),
            )
        finally:
            try:
                if acquired and user_id:
                    self._lock_service.release(db, user_id)
                    db.commit()
            finally:
                db.close()
