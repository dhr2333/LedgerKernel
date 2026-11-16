"""Subprocess adapter for the kernel CLI JSON contract.

The frontend never imports ledger_kernel. Commands have one fixed shape:
<kernel_cmd> --ledger <ledger> --json <subcommand...>. --confirm is added only
from an explicit caller argument; approval is never implicit or automatic.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from .model import confirm_string

class KernelCliError(RuntimeError):
    """The kernel CLI failed, or its JSON output could not be used."""
    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code

@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""

Runner = Callable[[Sequence[str]], CommandResult]

DEFAULT_TIMEOUT_SECONDS = 120

def subprocess_runner(argv: Sequence[str], *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> CommandResult:
    try:
        done = subprocess.run(list(argv), capture_output=True, text=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        return CommandResult(124, "", f"kernel CLI timed out after {timeout:g}s")
    return CommandResult(done.returncode, done.stdout, done.stderr)

def _loads(text: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None

def _error(payload: Any) -> KernelCliError | None:
    """Map an {"ok": false, "error": {...}} payload to an exception."""
    if not isinstance(payload, dict) or payload.get("ok") is not False:
        return None
    error = payload.get("error")
    if not isinstance(error, dict):
        return KernelCliError("kernel CLI reported an error without details")
    message = str(error.get("message") or error.get("code") or "kernel CLI error")
    return KernelCliError(message, code=error.get("code"))

class KernelCli:
    def __init__(self, *, ledger: str, kernel_cmd: str = "ledger-kernel", runner: Runner | None = None) -> None:
        self.ledger = ledger
        self.kernel_cmd = kernel_cmd
        self.runner = runner or subprocess_runner
    def _call(self, *subcommand: str) -> Any:
        result = self.runner([self.kernel_cmd, "--ledger", self.ledger, "--json", *subcommand])
        payload = _loads(result.stdout)
        if result.returncode != 0:
            error = _error(payload) or _error(_loads(result.stderr))
            if error is not None:
                raise error
            lines = [line.strip() for line in (result.stderr or result.stdout).splitlines() if line.strip()]
            raise KernelCliError(f"kernel CLI exited with {result.returncode}" + (f": {lines[-1]}" if lines else ""))
        if payload is None:
            raise KernelCliError(f"invalid JSON from kernel CLI for {' '.join(subcommand)}")
        error = _error(payload)
        if error is not None:
            raise error
        return payload
    def _dict(self, *subcommand: str) -> dict[str, Any]:
        payload = self._call(*subcommand)
        if not isinstance(payload, dict):
            raise KernelCliError(f"unexpected kernel CLI payload for {' '.join(subcommand)}")
        return payload
    def review_queue(self) -> dict[str, Any]:
        return self._dict("review")
    def review_by_account(self) -> dict[str, Any]:
        return self._dict("review", "--by-account")
    def draft_detail(self, draft_id: str) -> dict[str, Any]:
        drafts = self._dict("review", draft_id).get("drafts") or []
        if not drafts or not isinstance(drafts[0], dict):
            raise KernelCliError(f"kernel CLI returned no detail for draft {draft_id}")
        return drafts[0]
    def validate_draft(self, draft_id: str) -> dict[str, Any]:
        return self._dict("drafts", "validate", draft_id)
    def approve_drafts(self, draft_ids: Sequence[str], confirm: str) -> list[dict[str, Any]]:
        """Approve explicit ids; confirm must equal the comma-joined draft ids."""
        ids = [str(draft_id) for draft_id in draft_ids]
        if not ids:
            raise KernelCliError("approval requires at least one explicit draft id")
        expected = confirm_string(ids)
        if confirm != expected:
            raise KernelCliError(f"confirmation must equal {expected!r}; approval aborted", code="INVALID_STATE")
        payload = self._call("drafts", "approve", *ids, "--confirm", confirm)
        if not isinstance(payload, list):
            raise KernelCliError("unexpected kernel CLI payload for drafts approve")
        return payload
