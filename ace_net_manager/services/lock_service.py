from __future__ import annotations

import hashlib
import threading

from sqlalchemy import text
from sqlalchemy.orm import Session


class LockService:
    def __init__(self) -> None:
        self._local_mutex = threading.Lock()
        self._local_locks: set[int] = set()

    @staticmethod
    def lock_key(user_id: str) -> int:
        digest = hashlib.sha256(str(user_id).encode("utf-8")).digest()[:8]
        return int.from_bytes(digest, byteorder="big", signed=True)

    def _is_postgres(self, db: Session) -> bool:
        try:
            return str(db.bind.dialect.name).startswith("postgresql")  # type: ignore[union-attr]
        except Exception:
            return False

    def try_acquire(self, db: Session, user_id: str) -> bool:
        key = self.lock_key(user_id)
        if self._is_postgres(db):
            row = db.execute(text("select pg_try_advisory_lock(:k)"), {"k": key}).scalar()
            return bool(row)

        with self._local_mutex:
            if key in self._local_locks:
                return False
            self._local_locks.add(key)
            return True

    def release(self, db: Session, user_id: str) -> None:
        key = self.lock_key(user_id)
        if self._is_postgres(db):
            db.execute(text("select pg_advisory_unlock(:k)"), {"k": key})
            return
        with self._local_mutex:
            self._local_locks.discard(key)

