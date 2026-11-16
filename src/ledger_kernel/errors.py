"""Kernel error types and stable error codes."""

from __future__ import annotations

from typing import Any


class KernelError(Exception):
    """An error with a stable machine-readable code.

    ``code`` is part of the public contract used by the CLI, MCP adapter and
    tests. Messages are human explanatory text and may change.
    """

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"[{self.code}] {self.message}"


# Codes used across the kernel. Keeping them in one place makes CLI/MCP output
# and tests stable.
CONFIG_INVALID = "CONFIG_INVALID"
LEDGER_NOT_FOUND = "LEDGER_NOT_FOUND"
LEDGER_LOAD_FAILED = "LEDGER_LOAD_FAILED"
LEDGER_INVALID = "LEDGER_INVALID"
CLOSURE_INCONSISTENT = "CLOSURE_INCONSISTENT"
WRITE_TARGET_INVALID = "WRITE_TARGET_INVALID"
KERNEL_BUSY = "KERNEL_BUSY"
SCHEMA_INVALID = "SCHEMA_INVALID"
UNSAFE_TEXT = "UNSAFE_TEXT"
DRAFT_NOT_FOUND = "DRAFT_NOT_FOUND"
VALIDATION_NOT_FOUND = "VALIDATION_NOT_FOUND"
INVALID_STATE = "INVALID_STATE"
STALE_REVISION = "STALE_REVISION"
APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
CONTRACT_INVALID = "CONTRACT_INVALID"
CONTRACT_CONFLICT = "CONTRACT_CONFLICT"
TEMPLATE_INVALID = "TEMPLATE_INVALID"
EVIDENCE_INVALID = "EVIDENCE_INVALID"
FRAGMENT_MISMATCH = "FRAGMENT_MISMATCH"
DELTA_MISMATCH = "DELTA_MISMATCH"
EXTERNAL_CHANGE = "EXTERNAL_CHANGE"
RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
AUDIT_INTEGRITY = "AUDIT_INTEGRITY"
IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
INTERNAL_ERROR = "INTERNAL_ERROR"
