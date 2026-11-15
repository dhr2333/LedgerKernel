"""Minimal but complete CLI for the v0.1 review loop.

Commands never auto-approve: ``drafts approve`` requires an explicit
``--confirm`` value (or an interactive prompt) naming the draft(s).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from typing import Any, Sequence

from . import __version__
from .errors import KernelError
from .kernel import Kernel


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)


def _print(value: Any, *, as_json: bool, text: str | None = None) -> None:
    if as_json:
        print(_json_dump(value))
    else:
        print(text if text is not None else _json_dump(value))


def _error(exc: KernelError, *, as_json: bool) -> int:
    if as_json:
        print(_json_dump({"ok": False, "error": exc.to_dict()}))
    else:
        print(f"[{exc.code}] {exc.message}", file=sys.stderr)
        if exc.details:
            print(_json_dump(exc.details), file=sys.stderr)
    return 2


def _require_confirm(draft_ids: Sequence[str], confirm: str | None) -> None:
    expected = ",".join(draft_ids)
    if confirm is not None:
        if confirm.strip() != expected:
            raise KernelError(
                "INVALID_STATE",
                "confirmation does not match the draft id(s); approval aborted",
                {"expected": expected},
            )
        return
    if not sys.stdin.isatty():
        raise KernelError(
            "INVALID_STATE",
            "approval requires --confirm with the exact draft id(s) in non-interactive mode",
            {"expected": expected},
        )
    entered = input(f"Type the draft id(s) to approve [{expected}]: ").strip()
    if entered != expected:
        raise KernelError("INVALID_STATE", "confirmation did not match; approval aborted")


def _status_text(status: dict[str, Any]) -> str:
    lines = [
        f"ledger        {status['ledger_id']}  ({status['ledger_dir']})",
        f"root          {status['root_file']}",
        f"write_target  {status['write_target']}",
        f"revision      {status['ledger_revision']}",
        f"contract      {status['contract_revision']}",
        f"engine        {status['engine_fingerprint']}",
        f"status        {status['status']}",
        f"load          {'ok' if status['ledger_load_ok'] else 'errors=' + str(status['beancount_error_count'])}",
        f"closure       {status['files_in_closure']} files via {status['closure_source']}",
        f"drafts        {status['draft_counts']}",
        f"observations  {status['observations']}",
        f"recovery      {status['pending_recovery'] or 'none'}",
        f"audit         {'ok' if status['audit_integrity']['ok'] else status['audit_integrity']['error']}",
    ]
    if status.get("contract_error"):
        lines.append(f"contract err  {status['contract_error']['message']}")
    return "\n".join(lines)


def _account_text(account: dict[str, Any]) -> str:
    balances = ", ".join(f"{amount} {currency}" for currency, amount in account.get("balances", {}).items()) or "-"
    return f"{account['account']}  {account.get('lifecycle')}  {balances}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ledger-kernel", description="Local account kernel for a Beancount ledger")
    parser.add_argument("--ledger", default=".", help="ledger directory containing .ledger-kernel (default: .)")
    parser.add_argument("--json", action="store_true", help="machine-readable JSON output")
    parser.add_argument("--version", action="version", version=f"ledger-kernel {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="bind a Beancount root file and a write target")
    init.add_argument("root_file")
    init.add_argument("--write-target", required=True)
    init.add_argument("--ledger-id")
    init.add_argument("--no-carry-forward", action="store_true")

    sub.add_parser("status", help="show kernel status and revision")

    accounts = sub.add_parser("accounts", help="account queries")
    accounts_sub = accounts.add_subparsers(dest="accounts_command", required=True)
    accounts_list = accounts_sub.add_parser("list")
    accounts_list.add_argument("--as-of")
    accounts_show = accounts_sub.add_parser("show")
    accounts_show.add_argument("account")
    accounts_show.add_argument("--as-of")
    accounts_balance = accounts_sub.add_parser("balance")
    accounts_balance.add_argument("account")
    accounts_balance.add_argument("--as-of")

    drafts = sub.add_parser("drafts", help="draft lifecycle")
    drafts_sub = drafts.add_subparsers(dest="drafts_command", required=True)
    drafts_list = drafts_sub.add_parser("list")
    drafts_list.add_argument("--state")
    drafts_show = drafts_sub.add_parser("show")
    drafts_show.add_argument("draft_id")
    drafts_validate = drafts_sub.add_parser("validate")
    drafts_validate.add_argument("draft_id")
    drafts_diff = drafts_sub.add_parser("diff")
    drafts_diff.add_argument("draft_id")
    drafts_diff.add_argument("--validation")
    drafts_approve = drafts_sub.add_parser("approve")
    drafts_approve.add_argument("draft_ids", nargs="+")
    drafts_approve.add_argument("--confirm")
    drafts_approve.add_argument("--note")
    drafts_reject = drafts_sub.add_parser("reject")
    drafts_reject.add_argument("draft_id")
    drafts_reject.add_argument("--reason", required=True)
    drafts_withdraw = drafts_sub.add_parser("withdraw")
    drafts_withdraw.add_argument("draft_id")
    drafts_withdraw.add_argument("--agent")
    drafts_commit = drafts_sub.add_parser("commit")
    drafts_commit.add_argument("draft_ids", nargs="+")

    sub.add_parser("scan", help="rescan the ledger and record external changes")

    corrections = sub.add_parser("corrections", help="correction proposals")
    corrections_sub = corrections.add_subparsers(dest="corrections_command", required=True)
    corrections_propose = corrections_sub.add_parser("propose", help="propose a reversal draft for a kernel commit")
    corrections_propose.add_argument("commit_id")

    observations = sub.add_parser("observations", help="external change observations")
    observations_sub = observations.add_subparsers(dest="observations_command", required=True)
    observations_sub.add_parser("list")
    observations_show = observations_sub.add_parser("show")
    observations_show.add_argument("observation_id")

    recovery = sub.add_parser("recovery", help="commit journal recovery")
    recovery_sub = recovery.add_subparsers(dest="recovery_command", required=True)
    recovery_sub.add_parser("status")
    recovery_recover = recovery_sub.add_parser("recover")
    recovery_recover.add_argument("commit_id", nargs="?")

    audit = sub.add_parser("audit", help="show recent audit events")
    audit.add_argument("--limit", type=int, default=20)
    return parser


def _as_date(value: str | None) -> dt.date | None:
    if value is None:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise KernelError("SCHEMA_INVALID", f"invalid date: {value}") from exc


def run(args: argparse.Namespace) -> Any:
    if args.command == "init":
        kernel = Kernel.init(
            args.root_file,
            args.write_target,
            ledger_id=args.ledger_id,
            approval_carry_forward=not args.no_carry_forward,
        )
        return kernel.get_status()
    kernel = Kernel.open(args.ledger)
    if args.command == "status":
        return kernel.get_status()
    if args.command == "scan":
        return kernel.scan_ledger()
    if args.command == "corrections":
        if args.corrections_command == "propose":
            return kernel.propose_reversal(args.commit_id)
        raise KernelError("INVALID_STATE", f"unknown corrections command: {args.corrections_command}")
    if args.command == "accounts":
        if args.accounts_command == "list":
            return kernel.list_accounts(_as_date(args.as_of))
        if args.accounts_command == "show":
            return kernel.get_account(args.account, _as_date(args.as_of))
        if args.accounts_command == "balance":
            return kernel.get_balance(args.account, _as_date(args.as_of))
    if args.command == "drafts":
        if args.drafts_command == "list":
            return kernel.list_drafts(state=args.state)
        if args.drafts_command == "show":
            return kernel.get_draft(args.draft_id)
        if args.drafts_command == "validate":
            return kernel.validate_draft(args.draft_id)
        if args.drafts_command == "diff":
            return kernel.get_draft_diff(args.draft_id, args.validation)
        if args.drafts_command == "approve":
            _require_confirm(args.draft_ids, args.confirm)
            return kernel.approve_drafts(args.draft_ids, note=args.note)
        if args.drafts_command == "reject":
            return kernel.reject_draft(args.draft_id, args.reason)
        if args.drafts_command == "withdraw":
            return kernel.withdraw_draft(args.draft_id, agent_id=args.agent)
        if args.drafts_command == "commit":
            return kernel.commit_drafts(args.draft_ids)
    if args.command == "observations":
        if args.observations_command == "list":
            return kernel.list_observations()
        return kernel.get_observation(args.observation_id)
    if args.command == "recovery":
        if args.recovery_command == "status":
            return kernel.recovery_status()
        return kernel.recover(args.commit_id)
    if args.command == "audit":
        return kernel.audit_tail(args.limit)
    raise KernelError("INVALID_STATE", f"unknown command: {args.command}")


def _render(args: argparse.Namespace, result: Any) -> str | None:
    if args.command == "status" and isinstance(result, dict) and "ledger_id" in result:
        return _status_text(result)
    if args.command == "accounts" and args.accounts_command == "list":
        return "\n".join(_account_text(row) for row in result) or "no accounts"
    if args.command == "drafts" and args.drafts_command == "list":
        rows = [
            f"{row['draft_id']}  {row['state']:<20} stale={str(row['stale']).lower():<5} "
            f"{row['agent']}  {row['narration']}"
            for row in result
        ]
        return "\n".join(rows) or "no drafts"
    if args.command == "observations" and args.observations_command == "list":
        rows = [
            f"{row['observation_id']}  {row['detected_at']}  {row['source_kind']}/{row['attribution_confidence']}  "
            f"{', '.join(row['changed_files'])}"
            for row in result
        ]
        return "\n".join(rows) or "no observations"
    if args.command == "audit":
        return "\n".join(f"{row['occurred_at']}  {row['event_type']}  {row['event_id']}" for row in result)
    return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = run(args)
        _print(result, as_json=args.json, text=_render(args, result))
        return 0
    except KernelError as exc:
        return _error(exc, as_json=args.json)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
