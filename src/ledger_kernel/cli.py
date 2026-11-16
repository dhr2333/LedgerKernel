"""Minimal but complete CLI for the v0.1 review loop.

Commands never auto-approve: ``drafts approve`` requires an explicit
``--confirm`` value (or an interactive prompt) naming the draft(s).

``review --by-account`` is a read-only grouped view (ADR-020): it aggregates
pending drafts by account but never adds an account-level approval path.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .errors import CONTRACT_CONFLICT, CONTRACT_INVALID, RECONCILIATION_INVALID, KernelError
from .kernel import Kernel
from .templates import build_payload, load_templates
from .util import canonical_json_bytes, sha256_bytes


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


def _require_explicit_confirm(expected: str, confirm: str | None, *, action: str) -> None:
    """Require an exact human confirmation; never auto-confirm in non-interactive mode."""
    if confirm is not None:
        if confirm.strip() != expected:
            raise KernelError(
                "INVALID_STATE",
                f"confirmation does not match the {action}; aborted",
                {"expected": expected},
            )
        return
    if not sys.stdin.isatty():
        raise KernelError(
            "INVALID_STATE",
            f"{action} requires --confirm with the exact value in non-interactive mode",
            {"expected": expected},
        )
    entered = input(f"Type {expected!r} to {action}: ").strip()
    if entered != expected:
        raise KernelError("INVALID_STATE", "confirmation did not match; aborted")


def _parse_declared(values: Sequence[str] | None) -> dict[str, Decimal] | None:
    """Parse repeatable ``--declared CUR=VALUE`` into Decimal amounts."""
    if not values:
        return None
    declared: dict[str, Decimal] = {}
    for item in values:
        currency, separator, raw = str(item).partition("=")
        currency = currency.strip()
        if not separator or not currency or not raw.strip():
            raise KernelError(
                RECONCILIATION_INVALID,
                f"--declared must be CUR=VALUE: {item!r}",
            )
        if currency in declared:
            raise KernelError(
                RECONCILIATION_INVALID, f"duplicate --declared currency: {currency}"
            )
        try:
            amount = Decimal(raw.strip())
        except (InvalidOperation, ValueError):
            raise KernelError(
                RECONCILIATION_INVALID,
                f"--declared value is not a decimal amount: {raw!r}",
            ) from None
        if not amount.is_finite():
            raise KernelError(
                RECONCILIATION_INVALID, f"--declared value must be finite: {raw!r}"
            )
        declared[currency] = amount
    return declared


def _balances_text(balances: dict[str, Any] | None) -> str:
    return ", ".join(f"{amount} {currency}" for currency, amount in sorted((balances or {}).items())) or "-"


def _reconcile_text(result: dict[str, Any]) -> str:
    record = result["record"]
    view = result["reconciliation"]
    lines = [
        f"reconciled    {record['reconciliation_id']}  {record['account']}  as_of {record['as_of']}",
        f"ledger        {_balances_text(record.get('ledger_balances'))}",
        f"declared      {_balances_text(record.get('declared_balances'))}",
        f"differences   {_balances_text(record.get('differences'))}",
        f"status        {view['status']}  difference={view['difference_status']}  "
        f"next_due={view['next_due'] or '-'}",
        f"ledger rev    {record['ledger_revision']}",
        f"audit event   {result['audit_event_id']}",
    ]
    if record.get("declared_not_in_ledger"):
        lines.insert(4, "not in ledger " + ", ".join(record["declared_not_in_ledger"]))
    if record.get("note"):
        lines.append(f"note          {record['note']}")
    return "\n".join(lines)


def _reconciliation_list_text(rows: Sequence[dict[str, Any]]) -> str:
    lines = [
        f"{row['reconciliation_id']}  as_of {row['as_of']}  recorded {row['recorded_at']}  "
        f"ledger {_balances_text(row.get('ledger_balances'))}  "
        f"declared {_balances_text(row.get('declared_balances'))}  "
        f"differences {_balances_text(row.get('differences'))}"
        for row in rows
    ]
    return "\n".join(lines) or "no reconciliations"


def _revert_text(result: dict[str, Any]) -> str:
    view = result["reconciliation"]
    return (
        f"reverted      {result['reconciliation_id']}  {result['account']}  as_of {result['as_of']}\n"
        f"reason        {result['reason']}\n"
        f"status        {view['status']}  difference={view['difference_status']}  "
        f"next_due={view['next_due'] or '-'}\n"
        f"audit event   {result['audit_event_id']}"
    )


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


def _coverage_text(coverage: dict[str, Any]) -> str:
    ratio = coverage.get("ratio")
    ratio_text = "-" if ratio is None else f"{ratio:.2%}"
    return (
        f"active {coverage.get('active_account_count', 0)}  "
        f"managed {len(coverage.get('managed_accounts') or [])}  "
        f"unmanaged {len(coverage.get('unmanaged_accounts') or [])}  "
        f"uncovered {len(coverage.get('uncovered_accounts') or [])}  "
        f"ratio {ratio_text}"
    )


def _registry_text(view: dict[str, Any]) -> str:
    if not view.get("valid"):
        error = view.get("error") or {}
        return f"invalid registry  [{error.get('code', 'REGISTRY_INVALID')}] {error.get('message', '')}"
    counts = view["counts"]
    lines = [
        f"registry      {view['registry_revision']}",
        f"groups        {counts['groups']}",
        f"accounts      {counts['accounts']}  "
        f"(active {counts['active']} / archived {counts['archived']})",
    ]
    for account in view["accounts"]:
        lines.append(
            f"{account['alias']}  ->  {account['account']}  "
            f"[{account['status']}/{account['mode']}]"
        )
    lines.append(f"unmanaged     {', '.join(view['unmanaged_accounts']) or '-'}")
    if view.get("warnings"):
        lines.append(f"warnings      {len(view['warnings'])}")
    return "\n".join(lines)


def _registry_plan_text(plan: dict[str, Any]) -> str:
    counts = plan["counts"]
    lines = [
        f"registry      {plan['registry_revision']}",
        f"ledger        {plan['ledger_revision']}",
        f"differences   {sum(counts.values())}  "
        f"({', '.join(f'{code} {count}' for code, count in counts.items()) or 'none'})",
    ]
    for item in plan["items"]:
        alias = f"  {item['alias']}" if item.get("alias") else ""
        lines.append(f"[{item['severity']}] {item['code']}  {item['account']}{alias}  {item['message']}")
    actions = plan["actions"]
    lines.append(
        "actions       "
        f"open {len(actions['open'])}  close {len(actions['close'])}  relabel {len(actions['relabel'])}"
    )
    return "\n".join(lines)


def _registry_resolve_text(result: dict[str, Any]) -> str:
    if not result.get("found"):
        suffix = " (ledger account, not in registry)" if result.get("unmanaged") else ""
        return f"not found: {result['query']}{suffix}"
    if result["kind"] == "account":
        return (
            f"{result['alias']}  ->  {result['account']}  "
            f"[{result['status']}]  id={result['id']}"
        )
    return f"group {result['path']}  alias={result['alias']}  id={result['id']}"


def _contracts_show_text(view: dict[str, Any]) -> str:
    lines = [
        f"contract      {view['contract_revision']}",
        f"valid         {'yes' if view['valid'] else 'no'}",
        f"accounts      {view['account_count']}",
        f"unmanaged     {', '.join(view['unmanaged_accounts']) or '-'}",
        f"coverage      {_coverage_text(view['coverage'])}",
    ]
    error = view.get("error")
    if error:
        lines.append(f"error         [{error['code']}] {error['message']}")
    for warning in view.get("warnings") or []:
        lines.append(f"warning       {warning['code']}: {warning['message']}")
    return "\n".join(lines)


def _contracts_preview_text(preview: dict[str, Any]) -> str:
    lines = [
        f"revision      {preview['current_revision']} -> {preview['contract_revision']}",
        f"changed       {'yes' if preview['changed'] else 'no'}",
        f"accounts      {preview['account_count']}",
        f"unmanaged     {', '.join(preview['unmanaged_accounts']) or '-'}",
        f"coverage      {_coverage_text(preview['coverage'])}",
    ]
    for warning in preview.get("warnings") or []:
        lines.append(f"warning       {warning['code']}: {warning['message']}")
    affected = preview.get("affected") or {}
    account_changes = affected.get("accounts") or {}
    for key, label in (("added", "+"), ("removed", "-"), ("modified", "~")):
        if account_changes.get(key):
            lines.append(f"accounts {label}    {', '.join(account_changes[key])}")
    unmanaged_changes = affected.get("unmanaged") or {}
    for key, label in (("added", "+"), ("removed", "-")):
        if unmanaged_changes.get(key):
            lines.append(f"unmanaged {label}   {', '.join(unmanaged_changes[key])}")
    diff = preview.get("diff")
    if diff:
        lines.append("")
        lines.extend(diff.splitlines()[:80])
    return "\n".join(lines)


def _review_group_header(group: dict[str, Any]) -> str:
    reconciliation = group["reconciliation"]
    next_due = reconciliation.get("next_due") or "-"
    header = (
        f"== {group['account']}  {group['lifecycle']}  {_balances_text(group['balances'])}  "
        f"reconciliation {reconciliation.get('status')} (next {next_due})"
    )
    difference = reconciliation.get("difference_status")
    if difference not in (None, "none"):
        header += f"  difference {difference}"
    return header


def _review_contract_text(contract: dict[str, Any] | None) -> str:
    if contract is None:
        return "   contract: none"
    agents = ", ".join(contract.get("allowed_agents") or []) or "-"
    currencies = ", ".join(contract.get("allowed_currencies") or []) or "-"
    return f"   contract: yes  allowed_agents: {agents}  allowed_currencies: {currencies}"


def _review_by_account_text(payload: dict[str, Any]) -> str:
    if not payload["groups"]:
        if payload.get("account"):
            return payload.get("note") or f"no pending drafts for {payload['account']}"
        return "no pending drafts"
    blocks = []
    for group in payload["groups"]:
        lines = [_review_group_header(group), _review_contract_text(group["contract"])]
        if group["pending_drafts"]:
            for row in group["pending_drafts"]:
                lines.append(
                    f"   pending: {row['draft_id']}  {row.get('date') or '-'}  "
                    f"{row.get('amount') or '-'} {row.get('currency') or ''}  "
                    f"{row.get('narration') or '-'}  "
                    f"[{row.get('state')} / {row.get('validation_status') or 'unvalidated'}]"
                )
        else:
            lines.append("   pending: none")
        for warning in group["warnings"]:
            lines.append(
                f"   warning: {warning['code']}  {warning['message']}  [{warning['draft_id']}]"
            )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


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
    accounts_list.add_argument(
        "--coverage", action="store_true", help="include contract coverage and unmanaged accounts"
    )
    accounts_show = accounts_sub.add_parser("show")
    accounts_show.add_argument("account")
    accounts_show.add_argument("--as-of")
    accounts_balance = accounts_sub.add_parser("balance")
    accounts_balance.add_argument("account")
    accounts_balance.add_argument("--as-of")
    accounts_sub.add_parser(
        "registry", help="show the account registry (accounts.yaml, schema v2 draft)"
    )
    accounts_sub.add_parser(
        "plan", help="diff the account registry against the ledger (read-only)"
    )
    accounts_resolve = accounts_sub.add_parser(
        "resolve", help="resolve an alias, id, account name or group path"
    )
    accounts_resolve.add_argument("name")
    accounts_reconcile = accounts_sub.add_parser(
        "reconcile", help="record an account reconciliation (human confirmed)"
    )
    accounts_reconcile.add_argument("account")
    accounts_reconcile.add_argument("--as-of", required=True)
    accounts_reconcile.add_argument(
        "--declared",
        action="append",
        default=[],
        metavar="CUR=VALUE",
        help="declared end-of-day balance; repeat for multiple currencies",
    )
    accounts_reconcile.add_argument("--note")
    accounts_reconcile.add_argument("--confirm", help="must equal the account name")
    accounts_reconciliation = accounts_sub.add_parser(
        "reconciliation", help="inspect and revert account reconciliations"
    )
    accounts_reconciliation_sub = accounts_reconciliation.add_subparsers(
        dest="reconciliation_command", required=True
    )
    accounts_reconciliation_list = accounts_reconciliation_sub.add_parser("list")
    accounts_reconciliation_list.add_argument("account")
    accounts_reconciliation_revert = accounts_reconciliation_sub.add_parser("revert")
    accounts_reconciliation_revert.add_argument("reconciliation_id")
    accounts_reconciliation_revert.add_argument("--reason", required=True)
    accounts_reconciliation_revert.add_argument(
        "--confirm", help="must equal the reconciliation id"
    )

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
    drafts_submit = drafts_sub.add_parser("submit", help="submit a JSON payload or expand a template")
    drafts_submit.add_argument("--payload", help="path to a draft payload JSON file")
    drafts_submit.add_argument("--template", help="template name from templates.yaml")
    drafts_submit.add_argument("--amount")
    drafts_submit.add_argument("--date")
    drafts_submit.add_argument("--key", help="idempotency key (defaults to a content hash)")
    drafts_submit.add_argument("--agent", default="cli-agent")
    drafts_submit.add_argument("--payee")
    drafts_submit.add_argument("--narration")
    drafts_submit.add_argument("--tag", action="append", default=[])

    templates = sub.add_parser("templates", help="reusable draft templates")
    templates_sub = templates.add_subparsers(dest="templates_command", required=True)
    templates_sub.add_parser("list")
    templates_show = templates_sub.add_parser("show")
    templates_show.add_argument("name")

    contracts = sub.add_parser("contracts", help="inspect and safely edit account contracts")
    contracts_sub = contracts.add_subparsers(dest="contracts_command", required=True)
    contracts_sub.add_parser("show", help="show parsed contracts, revision and coverage")
    contracts_validate = contracts_sub.add_parser(
        "validate", help="strictly parse a candidate file; never writes"
    )
    contracts_validate.add_argument("--file", required=True)
    contracts_diff = contracts_sub.add_parser(
        "diff", help="unified diff against the current contracts; never writes"
    )
    contracts_diff.add_argument("--file", required=True)
    contracts_apply = contracts_sub.add_parser(
        "apply", help="atomically replace contracts.yaml under dual-hash CAS"
    )
    contracts_apply.add_argument("--file", required=True)
    contracts_apply.add_argument(
        "--expected-current",
        help="sha256:<revision> the caller read, or 'absent' when no contracts file exists",
    )
    contracts_apply.add_argument(
        "--confirm", help="sha256:<revision> of the new contracts file (human confirmation)"
    )
    contracts_apply.add_argument("--note")

    review = sub.add_parser("review", help="review pending drafts and approve a batch in one place")
    review.add_argument("draft_ids", nargs="*")
    review.add_argument("--state", help="limit the queue to one draft state")
    review.add_argument(
        "--by-account",
        action="store_true",
        help="group the pending queue by account (read-only; ADR-020)",
    )
    review.add_argument(
        "--account",
        help="only show the group for this account (implies --by-account; may be empty)",
    )
    review.add_argument("--approve", action="store_true", help="approve the named drafts after confirmation")
    review.add_argument("--confirm")
    review.add_argument("--note")

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


def _read_contracts_candidate(path_value: str) -> str:
    path = Path(path_value)
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise KernelError(
            CONTRACT_INVALID,
            f"cannot read candidate contracts file: {exc}",
            {"path": str(path)},
        ) from exc


_REVIEW_QUEUE_STATES = {"Submitted", "NeedsHumanApproval", "Blocked", "Approved"}


def _review_queue_rows(kernel: Kernel, state: str | None) -> list[dict[str, Any]]:
    """Pending queue rows shared by ``review`` and ``review --by-account``."""
    allowed = {state} if state else _REVIEW_QUEUE_STATES
    items = []
    for row in kernel.list_drafts():
        if row["state"] not in allowed:
            continue
        entry = kernel.get_draft(row["draft_id"])
        validation = entry.get("validation") or {}
        draft = entry["draft"]
        transactions = draft.get("transactions") or []
        first = transactions[0] if transactions else {}
        postings = first.get("postings") or []
        items.append(
            {
                **row,
                "date": first.get("date"),
                "payee": first.get("payee"),
                "amount": postings[0].get("units") if postings else None,
                "currency": postings[0].get("currency") if postings else None,
                "validation_status": validation.get("status"),
                "checks": validation.get("checks") or [],
            }
        )
    return items


def _review_payload(kernel: Kernel, draft_ids: Sequence[str], state: str | None) -> dict[str, Any]:
    """Read-only review view: pending queue or per-draft detail."""
    if draft_ids:
        items = []
        for draft_id in dict.fromkeys(draft_ids):
            entry = kernel.get_draft(draft_id)
            validation = entry.get("validation") or {}
            items.append(
                {
                    "draft_id": draft_id,
                    "state": entry["workflow"].get("state"),
                    "validation_status": validation.get("status"),
                    "checks": validation.get("checks") or [],
                    "fragment": validation.get("fragment"),
                    "diff": validation.get("diff") or {},
                    "draft": entry["draft"],
                }
            )
        return {"mode": "detail", "drafts": items}
    return {"mode": "queue", "drafts": _review_queue_rows(kernel, state)}


def _known_review_accounts(kernel: Kernel) -> set[str]:
    """Accounts a grouped review can describe even when no draft references them."""
    names = {row["account"] for row in kernel.list_accounts()}
    names.update((kernel.contracts_view().get("accounts") or {}).keys())
    for row in kernel.list_drafts():
        names.update(row.get("accounts") or [])
    return names


def _review_draft_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "draft_id": row["draft_id"],
        "date": row.get("date"),
        "amount": row.get("amount"),
        "currency": row.get("currency"),
        "narration": row.get("narration"),
        "state": row.get("state"),
        "validation_status": row.get("validation_status"),
    }


def _review_by_account_payload(
    kernel: Kernel, *, state: str | None, account: str | None
) -> dict[str, Any]:
    """Read-only grouped review view (ADR-020).

    Accounts are derived from the pending drafts' ``account_refs`` (a draft is
    listed under every account it references), so grouping never creates an
    account-level approval, state or audit event. ``--account`` shows that single
    group even without pending drafts; an account unknown to the ledger, the
    contracts and all drafts yields ``groups: []`` plus a stable note.
    """
    rows = _review_queue_rows(kernel, state)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        for account_name in row.get("accounts") or []:
            grouped.setdefault(account_name, []).append(row)

    if account is not None:
        if account not in grouped and account not in _known_review_accounts(kernel):
            return {
                "mode": "by_account",
                "account": account,
                "note": f"unknown account: {account}",
                "groups": [],
            }
        names = [account]
    else:
        names = sorted(grouped)

    # One bulk read provides lifecycle/balances/contract/reconciliation for all
    # groups; falling back to get_account only for accounts outside the ledger.
    account_rows = {row["account"]: row for row in kernel.list_accounts()}
    groups: list[dict[str, Any]] = []
    for name in names:
        drafts = grouped.get(name, [])
        meta = account_rows.get(name)
        if meta is not None:
            lifecycle = meta["lifecycle"]
            balances = meta["balances"]
            reconciliation = meta["reconciliation"]
            contract = meta["contract"]
        else:
            try:
                view = kernel.get_account(name)
                lifecycle = view["lifecycle"]
                balances = view["balances"]
                reconciliation = view["reconciliation"]
                contract = view["contract"]
            except KernelError as exc:
                if exc.code != "ACCOUNT_NOT_FOUND":
                    raise
                lifecycle, balances, contract = "not_open", {}, None
                reconciliation = {
                    "status": "not_configured",
                    "last": None,
                    "difference_status": "none",
                    "next_due": None,
                }
        warnings: list[dict[str, Any]] = []
        seen: set[tuple[str, str, Any]] = set()
        for row in drafts:
            for check in row.get("checks") or []:
                code = str(check.get("code") or "")
                # Contract warnings stay advisory in this view; never copied as blocks.
                if not code.startswith("CONTRACT_") or check.get("severity") != "warning":
                    continue
                # Attribute a check to the account it names instead of repeating it
                # under every group the draft appears in.
                if check.get("account") not in (None, name):
                    continue
                marker = (row["draft_id"], code, check.get("message"))
                if marker in seen:
                    continue
                seen.add(marker)
                warnings.append(
                    {
                        "draft_id": row["draft_id"],
                        "code": code,
                        "severity": check.get("severity"),
                        "account": check.get("account"),
                        "message": check.get("message"),
                    }
                )
        groups.append(
            {
                "account": name,
                "lifecycle": lifecycle,
                "balances": balances,
                "reconciliation": reconciliation,
                "contract": contract,
                "pending_drafts": [_review_draft_row(row) for row in drafts],
                "warnings": warnings,
                "draft_count": len(drafts),
            }
        )
    return {"mode": "by_account", "account": account, "note": None, "groups": groups}


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
            rows = kernel.list_accounts(_as_date(args.as_of))
            if not getattr(args, "coverage", False):
                return rows
            view = kernel.contracts_view()
            return {
                "accounts": rows,
                "coverage": view["coverage"],
                "unmanaged_accounts": view["unmanaged_accounts"],
            }
        if args.accounts_command == "show":
            return kernel.get_account(args.account, _as_date(args.as_of))
        if args.accounts_command == "balance":
            return kernel.get_balance(args.account, _as_date(args.as_of))
        if args.accounts_command == "registry":
            return kernel.registry_view()
        if args.accounts_command == "plan":
            return kernel.registry_plan()
        if args.accounts_command == "resolve":
            return kernel.resolve_account(args.name)
        if args.accounts_command == "reconcile":
            as_of = _as_date(args.as_of)
            if as_of is None:
                raise KernelError(RECONCILIATION_INVALID, "accounts reconcile requires --as-of")
            _require_explicit_confirm(
                args.account, args.confirm, action="reconciliation"
            )
            return kernel.reconcile_account(
                args.account,
                as_of=as_of,
                declared=_parse_declared(args.declared),
                note=args.note,
            )
        if args.accounts_command == "reconciliation":
            if args.reconciliation_command == "list":
                return kernel.list_reconciliations(args.account)
            if args.reconciliation_command == "revert":
                _require_explicit_confirm(
                    args.reconciliation_id,
                    args.confirm,
                    action="reconciliation revert",
                )
                return kernel.revert_reconciliation(
                    args.reconciliation_id, reason=args.reason
                )
    if args.command == "templates":
        available = load_templates(kernel.store.templates_path)
        if args.templates_command == "list":
            return [template.to_dict() for template in available.values()]
        template = available.get(args.name)
        if template is None:
            raise KernelError("TEMPLATE_INVALID", f"unknown template: {args.name}")
        return template.to_dict()
    if args.command == "contracts":
        if args.contracts_command == "show":
            return kernel.contracts_view()
        if args.contracts_command == "apply":
            if not args.expected_current:
                raise KernelError(
                    CONTRACT_CONFLICT,
                    "contracts apply requires --expected-current "
                    "(use 'absent' when no contracts file exists)",
                    {"hint": "run ledger-kernel contracts show to read the current revision"},
                )
            if not args.confirm:
                raise KernelError(
                    CONTRACT_CONFLICT,
                    "contracts apply requires --confirm with the SHA-256 of the new contracts file",
                )
        text = _read_contracts_candidate(args.file)
        if args.contracts_command == "validate":
            return {"mode": "validate", **kernel.preview_contracts(text)}
        if args.contracts_command == "diff":
            return {"mode": "diff", **kernel.preview_contracts(text)}
        return kernel.apply_contracts(
            text,
            expected_current=args.expected_current,
            confirm=args.confirm,
            note=args.note,
        )
    if args.command == "drafts":
        if args.drafts_command == "submit":
            if bool(args.payload) == bool(args.template):
                raise KernelError("INVALID_STATE", "use exactly one of --payload or --template")
            if args.template:
                available = load_templates(kernel.store.templates_path)
                template = available.get(args.template)
                if template is None:
                    raise KernelError("TEMPLATE_INVALID", f"unknown template: {args.template}")
                entry_date = _as_date(args.date) if args.date else dt.date.today()
                if entry_date is None:
                    raise KernelError("SCHEMA_INVALID", "--date is required with --template")
                payload = build_payload(
                    template,
                    amount=args.amount,
                    entry_date=entry_date,
                    payee=args.payee,
                    narration=args.narration,
                    tags=args.tag,
                )
            else:
                try:
                    payload = json.loads(Path(args.payload).read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise KernelError("SCHEMA_INVALID", f"cannot read payload: {exc}") from exc
            key = args.key or "cli:" + sha256_bytes(canonical_json_bytes(payload))[:32]
            return kernel.submit_draft(payload, key, agent_id=args.agent)
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
    if args.command == "review":
        if args.approve:
            if not args.draft_ids:
                raise KernelError("INVALID_STATE", "--approve requires explicit draft ids")
            _require_confirm(args.draft_ids, args.confirm)
            return {"approved": kernel.approve_drafts(args.draft_ids, note=args.note)}
        if args.by_account or args.account is not None:
            if args.draft_ids:
                raise KernelError(
                    "INVALID_STATE",
                    "review --by-account/--account shows a grouped view; "
                    "pass draft ids or the grouped view, not both",
                )
            return _review_by_account_payload(kernel, state=args.state, account=args.account)
        return _review_payload(kernel, args.draft_ids, args.state)
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
        if isinstance(result, dict):
            lines = [_account_text(row) for row in result["accounts"]] or ["no accounts"]
            lines.append("")
            lines.append(f"coverage      {_coverage_text(result['coverage'])}")
            lines.append(f"unmanaged     {', '.join(result['unmanaged_accounts']) or '-'}")
            return "\n".join(lines)
        return "\n".join(_account_text(row) for row in result) or "no accounts"
    if args.command == "accounts" and args.accounts_command == "registry":
        return _registry_text(result)
    if args.command == "accounts" and args.accounts_command == "plan":
        return _registry_plan_text(result)
    if args.command == "accounts" and args.accounts_command == "resolve":
        return _registry_resolve_text(result)
    if args.command == "accounts" and args.accounts_command == "reconcile":
        return _reconcile_text(result)
    if args.command == "accounts" and args.accounts_command == "reconciliation":
        if args.reconciliation_command == "list":
            return _reconciliation_list_text(result)
        return _revert_text(result)
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
    if args.command == "contracts":
        if args.contracts_command == "show":
            return _contracts_show_text(result)
        if args.contracts_command in {"validate", "diff"}:
            return _contracts_preview_text(result)
        if result.get("applied"):
            return (
                f"applied       {result['previous_revision']} -> {result['contract_revision']}\n"
                f"audit event   {result['audit_event_id']}"
            )
        return "no changes; nothing written"
    if args.command == "templates" and args.templates_command == "list":
        rows = [f"{template['name']:<20} {template['payee'] or '-':<24} {len(template['postings'])} postings" for template in result]
        return "\n".join(rows) or "no templates"
    if args.command == "templates" and args.templates_command == "show":
        return _json_dump(result)
    if args.command == "drafts" and args.drafts_command == "submit":
        return f"{result['draft']['draft_id']}  {result['workflow']['state']}"
    if args.command == "review":
        if "approved" in result:
            return "\n".join(
                f"{row['draft_id']}  {row['workflow'].get('state')}  validation={row['workflow'].get('validation_id')}"
                for row in result["approved"]
            ) or "no drafts approved"
        if result.get("mode") == "by_account":
            return _review_by_account_text(result)
        drafts = result["drafts"]
        if result["mode"] == "queue":
            lines = [f"{len(drafts)} draft(s) awaiting review"]
            for row in drafts:
                codes = ", ".join(str(check.get("code")) for check in row.get("checks") or []) or "no checks"
                lines.append(
                    f"{row['draft_id']}  {row['state']:<20} {str(row.get('validation_status')):<18} "
                    f"{row.get('date')}  {row.get('amount')} {row.get('currency')}  "
                    f"{row.get('payee') or '-'}  [{codes}]"
                )
            return "\n".join(lines)
        blocks = []
        for row in drafts:
            validation = row.get("validation_status")
            checks = row.get("checks") or []
            lines = [f"== {row['draft_id']}  {row['state']}  validation={validation}"]
            fragment = (row.get("fragment") or "").strip("\n")
            lines.append(fragment or "(no validated fragment)")
            if checks:
                lines.extend(
                    f"  [{check.get('severity')}] {check.get('code')}: {check.get('message')}" for check in checks
                )
            else:
                lines.append("  no blocking or warning checks")
            diff = row.get("diff") or {}
            if diff.get("changed_files"):
                lines.append("  files: " + ", ".join(diff["changed_files"]))
            for delta in diff.get("deltas") or []:
                lines.append(
                    f"  balance: {delta['account']}  {delta['before']} -> {delta['after']} "
                    f"({delta['delta']} {delta['currency']})"
                )
            file_diff = diff.get("file_diff")
            if file_diff:
                lines.append("  diff:")
                lines.extend("    " + line for line in str(file_diff).splitlines()[:30])
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)
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
