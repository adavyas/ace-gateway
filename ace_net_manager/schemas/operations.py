from __future__ import annotations

from typing import Literal

OperationType = Literal["new_user", "deploy", "rollback", "restart"]
OperationStatus = Literal["queued", "running", "succeeded", "failed", "rolled_back", "cancelled"]
RequestedBy = Literal["system", "admin", "gateway"]

