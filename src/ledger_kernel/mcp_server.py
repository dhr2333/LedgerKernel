"""Minimal MCP stdio adapter.

Only read and proposal tools are exposed. Approval, commit, contract changes
and raw file writes are intentionally absent: an Agent must never be able to
turn a proposal into a financial fact by itself.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, TextIO

from . import __version__
from .errors import KernelError
from .kernel import Kernel

PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "ledger_get_status",
        "description": "Read ledger binding, revision, load status and pending workflow counts.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "ledger_list_accounts",
        "description": "List accounts with lifecycle, balances, aliases and contract summaries.",
        "inputSchema": {
            "type": "object",
            "properties": {"as_of": {"type": "string", "description": "ISO date (optional)"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "ledger_get_account",
        "description": "Read one account definition, balances and contract.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "account": {"type": "string"},
                "as_of": {"type": "string"},
            },
            "required": ["account"],
            "additionalProperties": False,
        },
    },
    {
        "name": "ledger_get_balance",
        "description": "Read derived balances for one account.",
        "inputSchema": {
            "type": "object",
            "properties": {"account": {"type": "string"}, "as_of": {"type": "string"}},
            "required": ["account"],
            "additionalProperties": False,
        },
    },
    {
        "name": "ledger_submit_draft",
        "description": "Submit a structured transaction proposal. This never writes the ledger and never approves it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "payload": {"type": "object"},
                "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 200},
                "agent_id": {"type": "string"},
                "agent_version": {"type": "string"},
            },
            "required": ["payload", "idempotency_key"],
            "additionalProperties": False,
        },
    },
    {
        "name": "ledger_get_draft_status",
        "description": "Read a draft and its workflow/validation status.",
        "inputSchema": {
            "type": "object",
            "properties": {"draft_id": {"type": "string"}},
            "required": ["draft_id"],
            "additionalProperties": False,
        },
    },
]


class McpServer:
    def __init__(self, kernel: Kernel):
        self.kernel = kernel

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(message, dict):
            return _error_response(None, -32600, "Invalid Request")
        method = message.get("method")
        request_id = message.get("id")
        if method is None:
            return _error_response(request_id, -32600, "Invalid Request")
        if request_id is None and str(method).startswith("notifications/"):
            return None
        try:
            if method == "initialize":
                return _result(
                    request_id,
                    {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "ledger-kernel", "version": __version__},
                    },
                )
            if method == "ping":
                return _result(request_id, {})
            if method == "tools/list":
                return _result(request_id, {"tools": TOOLS})
            if method == "tools/call":
                params = message.get("params") or {}
                name = params.get("name")
                arguments = params.get("arguments") or {}
                if not isinstance(name, str) or not isinstance(arguments, dict):
                    return _error_response(request_id, -32602, "Invalid params")
                return _result(request_id, self._call_tool(name, arguments))
            return _error_response(request_id, -32601, f"Method not found: {method}")
        except KernelError as exc:
            return _error_response(request_id, -32000, exc.message, data=exc.to_dict())
        except Exception as exc:  # pragma: no cover - defensive boundary
            return _error_response(request_id, -32603, f"Internal error: {exc}")

    def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            if name == "ledger_get_status":
                value = self.kernel.get_status()
            elif name == "ledger_list_accounts":
                value = self.kernel.list_accounts(_date(arguments.get("as_of")))
            elif name == "ledger_get_account":
                value = self.kernel.get_account(str(arguments["account"]), _date(arguments.get("as_of")))
            elif name == "ledger_get_balance":
                value = self.kernel.get_balance(str(arguments["account"]), _date(arguments.get("as_of")))
            elif name == "ledger_submit_draft":
                payload = arguments.get("payload")
                key = arguments.get("idempotency_key")
                if not isinstance(payload, dict) or not isinstance(key, str):
                    raise KernelError("SCHEMA_INVALID", "payload and idempotency_key are required")
                result = self.kernel.submit_draft(
                    payload,
                    key,
                    agent_id=str(arguments.get("agent_id") or "mcp-agent"),
                    agent_version=str(arguments.get("agent_version") or "0.0.0"),
                )
                value = {
                    "draft_id": result["draft"]["draft_id"],
                    "state": result["workflow"]["state"],
                    "idempotent_replay": result.get("idempotent_replay", False),
                }
            elif name == "ledger_get_draft_status":
                entry = self.kernel.get_draft(str(arguments["draft_id"]))
                value = {
                    "draft_id": entry["draft"]["draft_id"],
                    "state": entry["workflow"].get("state"),
                    "stale": entry["workflow"].get("stale"),
                    "validation": None
                    if entry.get("validation") is None
                    else {
                        "validation_id": entry["validation"]["validation_id"],
                        "status": entry["validation"]["status"],
                        "checks": entry["validation"].get("checks"),
                    },
                }
            else:
                return {
                    "content": [{"type": "text", "text": f"unknown tool: {name}"}],
                    "isError": True,
                }
        except KernelError as exc:
            return {
                "content": [{"type": "text", "text": json.dumps(exc.to_dict(), ensure_ascii=False)}],
                "isError": True,
            }
        return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, default=str)}], "isError": False}


def _date(value: Any):
    if value in (None, ""):
        return None
    import datetime as dt

    try:
        return dt.date.fromisoformat(str(value))
    except ValueError as exc:
        raise KernelError("SCHEMA_INVALID", f"invalid as_of date: {value}") from exc


def _result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error_response(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def serve(kernel: Kernel, stdin: TextIO, stdout: TextIO) -> None:
    server = McpServer(kernel)
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            response = _error_response(None, -32700, "Parse error")
        else:
            response = server.handle(message)
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ledger-kernel-mcp", description="MCP stdio adapter for LedgerKernel")
    parser.add_argument("--ledger", default=".", help="ledger directory containing .ledger-kernel")
    args = parser.parse_args(argv)
    try:
        kernel = Kernel.open(args.ledger)
    except KernelError as exc:
        print(f"[{exc.code}] {exc.message}", file=sys.stderr)
        return 2
    serve(kernel, sys.stdin, sys.stdout)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
