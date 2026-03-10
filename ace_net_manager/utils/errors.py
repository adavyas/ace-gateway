from __future__ import annotations


class OperationError(Exception):
    def __init__(self, message: str, *, code: str = "operation_error"):
        super().__init__(message)
        self.code = code
        self.message = message


class OperationRolledBack(OperationError):
    def __init__(self, message: str = "operation rolled back"):
        super().__init__(message, code="rolled_back")


class OperationConflict(OperationError):
    def __init__(self, message: str = "another operation is in progress"):
        super().__init__(message, code="conflict")

