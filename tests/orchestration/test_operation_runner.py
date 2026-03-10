from __future__ import annotations

import asyncio

import pytest

from ace_net_manager.workers.operation_runner import OperationRunner


pytestmark = pytest.mark.unit


class _DummyDb:
    def __init__(self) -> None:
        self.commit_calls = 0
        self.rollback_calls = 0
        self.closed = False

    def commit(self) -> None:
        self.commit_calls += 1

    def rollback(self) -> None:
        self.rollback_calls += 1

    def close(self) -> None:
        self.closed = True


class _LockService:
    def __init__(self) -> None:
        self.released: list[str] = []

    def try_acquire(self, _db, _user_id: str) -> bool:
        return True

    def release(self, _db, user_id: str) -> None:
        self.released.append(user_id)


class _OperationService:
    def __init__(self) -> None:
        self.failed_calls: list[tuple[str, str]] = []
        self.cleanup_calls = 0

    def get_operation(self, _db, operation_id: str):
        return {
            "operation_id": operation_id,
            "user_id": "d6f543ce-3edd-449e-a8f7-c3a8f9016afe",
            "operation_type": "deploy",
            "status": "queued",
        }

    def mark_running(self, _db, _operation_id: str, step: str = "running") -> None:
        return

    async def execute(self, _db, _operation):
        await asyncio.sleep(2)
        return {"old_generation": 1, "new_generation": 2}

    async def handle_timeout_cleanup(self, _db, _operation) -> str:
        self.cleanup_calls += 1
        return "pre_cutover_candidate_removed"

    def mark_succeeded(self, _db, _operation_id: str, **_kwargs) -> None:
        raise AssertionError("mark_succeeded should not be called on timeout")

    def mark_rolled_back(self, _db, _operation_id: str, _message: str) -> None:
        raise AssertionError("mark_rolled_back should not be called on timeout")

    def mark_failed(self, _db, _operation_id: str, *, error_code: str, error_message: str) -> None:
        self.failed_calls.append((error_code, error_message))


@pytest.mark.asyncio
async def test_runner_marks_operation_failed_when_timeout_exceeded():
    op_service = _OperationService()
    lock_service = _LockService()

    def _session_factory() -> _DummyDb:
        return _DummyDb()

    runner = OperationRunner(
        session_factory=_session_factory,
        operation_service=op_service,
        lock_service=lock_service,
        max_concurrency=1,
        operation_timeout_seconds=1,
    )

    await runner.start()
    await runner.enqueue("op-timeout")

    await asyncio.sleep(1.5)
    await runner.shutdown()

    assert op_service.cleanup_calls == 1
    assert len(op_service.failed_calls) == 1
    code, message = op_service.failed_calls[0]
    assert code == "operation_timeout"
    assert "cleanup=pre_cutover_candidate_removed" in message
