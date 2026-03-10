from __future__ import annotations

from types import SimpleNamespace

from ace_net_manager.services.lock_service import LockService


class _FakeDb:
    def __init__(self) -> None:
        self.bind = SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))


def test_lock_service_local_fallback_single_holder():
    lock = LockService()
    db = _FakeDb()
    user_id = "8ab45f18-ecff-4e57-bf10-c4cff0defd4f"

    assert lock.try_acquire(db, user_id) is True
    assert lock.try_acquire(db, user_id) is False

    lock.release(db, user_id)
    assert lock.try_acquire(db, user_id) is True

