"""Dry-run in a throwaway ledger copy, delta assertion and review diff.

This is the security-critical path: a candidate is never evaluated as a
fragment alone. The *whole* ledger is loaded with the candidate appended, then
the delta is compared with the proposal entry by entry (ADR-012).
"""

from __future__ import annotations

import datetime as dt
import difflib
import os
import shutil
import tempfile
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping

from .adapter import BeancountAdapter, LoadResult
from .contracts import Contract, evaluate_contract
from .errors import DELTA_MISMATCH, FRAGMENT_MISMATCH, KernelError
from .schema import ParsedDraft, canonical_transaction, render_fragment
from .util import (
    canonical_json_bytes,
    encode_text_file,
    escape_display,
    is_within,
    read_text_file,
    safe_join,
    sha256_bytes,
    sha256_file,
)

FaultHook = Callable[[str, dict[str, Any]], Any]


@dataclass
class DryRunResult:
    status: str
    checks: list[dict[str, Any]]
    diff: dict[str, Any]
    fragment: str
    fragment_sha256: str
    review_digest: str
    candidate: LoadResult | None


def _check(
    checks: list[dict[str, Any]],
    code: str,
    severity: str,
    message: str,
    *,
    account: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    record = {"code": code, "severity": severity, "message": message, "account": account}
    if details:
        record["details"] = details
    checks.append(record)


def fragment_for(target_text: str, rendered: str, newline: str) -> str:
    """Return the exact bytes (as text) that will be appended.

    The fragment includes any leading blank-line separator, so the fragment
    fingerprint covers everything appended, not just the transaction block.
    """
    separator = ""
    if target_text:
        if not target_text.endswith(newline):
            separator += newline
        if not target_text.endswith(newline + newline):
            separator += newline
    return separator + rendered


def copy_ledger_workspace(load: LoadResult, temp_root: Path) -> None:
    """Copy the include closure and mirror everything else by symlink.

    Beancount ``Document`` directives and user plugins may reference files that
    are not part of the include closure (receipts, price files, ...). Symlinking
    the remaining top-level entries keeps the dry-run environment semantically
    close to the real ledger without copying large attachment trees.
    """
    for path in load.include_files:
        relative = path.relative_to(load.ledger_dir)
        destination = temp_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
    skip = {".ledger-kernel", ".git", ".stversions", ".stfolder", "__pycache__"}

    def mirror(source_dir: Path, destination_dir: Path) -> None:
        for child in source_dir.iterdir():
            if child.name in skip:
                continue
            destination = destination_dir / child.name
            if destination.is_symlink():
                continue
            if destination.exists():
                if child.is_dir() and destination.is_dir():
                    mirror(child, destination)
                continue
            try:
                os.symlink(child.resolve(), destination, target_is_directory=child.is_dir())
            except OSError:
                continue

    mirror(load.ledger_dir, temp_root)


def _copy_closure(load: LoadResult, temp_root: Path) -> None:
    copy_ledger_workspace(load, temp_root)


def _compare_delta(
    baseline: LoadResult,
    candidate: LoadResult,
    expected: list[tuple],
) -> tuple[bool, dict[str, Any]]:
    old_counter = baseline.entry_counter(baseline.entries)
    new_counter = candidate.entry_counter(candidate.entries)
    missing: list[Any] = []
    for key, count in old_counter.items():
        available = new_counter.get(key, 0)
        if available < count:
            missing.extend([key] * (count - available))
    added = new_counter - old_counter
    added_count = sum(added.values())
    expected_counter = Counter(expected)
    details: dict[str, Any] = {
        "missing_or_modified": len(missing),
        "added_entries": added_count,
        "expected_entries": len(expected),
    }
    if missing:
        details["reason"] = "existing entries disappeared or were modified"
        return False, details
    if added != expected_counter:
        extra = sum((added - expected_counter).values())
        absent = sum((expected_counter - added).values())
        details["reason"] = "added entries differ from the proposal"
        details["unexpected_added"] = extra
        details["missing_expected"] = absent
        return False, details
    return True, details


def _check_options(baseline: LoadResult, candidate: LoadResult) -> list[str]:
    before = baseline.options_signature(baseline.options)
    after = candidate.options_signature(candidate.options)
    changed: list[str] = []
    for key in sorted(set(before) | set(after)):
        if before.get(key) != after.get(key):
            changed.append(key)
    return changed


def _check_other_files(baseline: LoadResult, candidate: LoadResult, write_target: str) -> list[str]:
    changed: list[str] = []
    paths = set(baseline.manifest) | set(candidate.manifest)
    for relative in sorted(paths):
        if relative == write_target:
            continue
        if baseline.manifest.get(relative) != candidate.manifest.get(relative):
            changed.append(relative)
    return changed


def _evidence_checks(ledger_dir: Path, parsed: ParsedDraft) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for evidence in parsed.evidence:
        if evidence.kind == "file":
            try:
                path = safe_join(ledger_dir, evidence.locator)
            except KernelError:
                _check(
                    checks,
                    "EVIDENCE_INVALID",
                    "block",
                    f"evidence locator escapes the ledger directory: {escape_display(evidence.locator)}",
                )
                continue
            if not path.is_file():
                _check(
                    checks,
                    "EVIDENCE_MISSING",
                    "warning",
                    f"evidence file is missing: {escape_display(evidence.locator)}",
                )
                continue
            if evidence.sha256 is None:
                _check(
                    checks,
                    "EVIDENCE_NO_HASH",
                    "warning",
                    f"evidence file has no sha256: {escape_display(evidence.locator)}",
                )
            elif sha256_file(path) != evidence.sha256:
                _check(
                    checks,
                    "EVIDENCE_HASH_MISMATCH",
                    "warning",
                    f"evidence file hash does not match the proposal: {escape_display(evidence.locator)}",
                )
        elif evidence.kind == "text" and evidence.sha256 is not None:
            if sha256_bytes(evidence.locator.encode("utf-8")) != evidence.sha256:
                _check(checks, "EVIDENCE_HASH_MISMATCH", "warning", "text evidence hash does not match")
    return checks


def _balance_diff(
    adapter: BeancountAdapter,
    baseline: LoadResult,
    candidate: LoadResult,
    parsed: ParsedDraft,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    as_of = max((txn.date for txn in parsed.transactions), default=dt.date.today())
    before = adapter.balances(baseline, as_of)
    after = adapter.balances(candidate, as_of)
    accounts = set(parsed.accounts)
    rows_before: list[dict[str, Any]] = []
    rows_after: list[dict[str, Any]] = []
    deltas: list[dict[str, Any]] = []
    for account in sorted(accounts):
        currencies = sorted({currency for (name, currency) in set(before) | set(after) if name == account})
        for currency in currencies:
            key = (account, currency)
            old = before.get(key, Decimal("0"))
            new = after.get(key, Decimal("0"))
            rows_before.append({"account": account, "currency": currency, "amount": format(old, "f")})
            rows_after.append({"account": account, "currency": currency, "amount": format(new, "f")})
            deltas.append(
                {
                    "account": account,
                    "currency": currency,
                    "before": format(old, "f"),
                    "after": format(new, "f"),
                    "delta": format(new - old, "f"),
                }
            )
    return rows_before, rows_after, deltas


def _file_diff(write_target: str, old_text: str, new_text: str) -> str:
    diff = difflib.unified_diff(
        old_text.splitlines(),
        new_text.splitlines(),
        fromfile=f"a/{write_target}",
        tofile=f"b/{write_target}",
        lineterm="",
        n=2,
    )
    lines = list(diff)
    if len(lines) > 400:
        lines = lines[:400] + ["... diff truncated ..."]
    return "\n".join(lines)


def review_digest(
    rendered_sha256: str,
    accounts: list[str],
    checks: list[dict[str, Any]],
) -> str:
    """Hash of what the human actually reviews.

    Deliberately hashes the rendered transactions, not the append fragment:
    the fragment includes a leading blank-line separator that depends on the
    current target text. An empty target gains that separator after the first
    commit, which used to change the digest and invalidate the rest of a batch
    (ADR-015). ``fragment_sha256`` still covers the exact appended bytes.
    """
    payload = {
        "rendered_sha256": rendered_sha256,
        "accounts": sorted(set(accounts)),
        "checks": sorted(
            (
                str(check.get("code")),
                str(check.get("account") or ""),
                str(check.get("severity")),
            )
            for check in checks
            # Advisory only: legitimately repeated entries must not lose an
            # already-reviewed approval mid-batch (ADR-017).
            if str(check.get("code")) != "DUPLICATE_SUSPECTED"
        ),
    }
    return "sha256:" + sha256_bytes(canonical_json_bytes(payload))



def _posting_signature(date, postings) -> tuple:
    items = []
    for posting in postings:
        units = getattr(posting, "units", None)
        if units is None:
            continue
        if hasattr(units, "number"):
            number, currency = units.number, str(units.currency)
        else:  # ParsedPosting stores a bare Decimal plus a currency field
            number, currency = units, str(getattr(posting, "currency", ""))
        items.append((str(posting.account), abs(number), currency))
    return (date, tuple(sorted(items)))


def _duplicate_checks(checks: list[dict[str, Any]], parsed: ParsedDraft, baseline: LoadResult) -> None:
    """Warn when the proposal repeats a transaction already in the ledger.

    Matches on date + account + absolute amount (payee may be anonymized), and
    on duplicates inside the same draft. Warning only: legitimate same-day
    repeats exist; the reviewer decides.
    """
    existing = set()
    for entry in baseline.entries:
        if entry.__class__.__name__ != "Transaction":
            continue
        existing.add(_posting_signature(entry.date, entry.postings))
    seen: set[tuple] = set()
    for transaction in parsed.transactions:
        signature = _posting_signature(transaction.date, transaction.postings)
        details = {
            "date": transaction.date.isoformat(),
            "postings": [[account, str(amount), currency] for account, amount, currency in signature[1]],
        }
        if signature in existing:
            _check(
                checks,
                "DUPLICATE_SUSPECTED",
                "warning",
                f"a transaction with the same date, accounts and amounts already exists ({transaction.date.isoformat()})",
                account=transaction.postings[0].account if transaction.postings else None,
                details=details,
            )
        elif signature in seen:
            _check(
                checks,
                "DUPLICATE_SUSPECTED",
                "warning",
                "the draft contains two transactions with the same date, accounts and amounts",
                account=transaction.postings[0].account if transaction.postings else None,
                details=details,
            )
        seen.add(signature)


def dry_run(
    *,
    ledger_dir: Path,
    write_target: str,
    baseline: LoadResult,
    adapter_factory: Callable[[Path], BeancountAdapter],
    parsed: ParsedDraft,
    agent_id: str,
    contract: Contract,
    reconciliation_status: Mapping[str, str] | None = None,
    fault_hook: FaultHook | None = None,
) -> DryRunResult:
    checks: list[dict[str, Any]] = []
    raw_target = ledger_dir / write_target
    if raw_target.is_symlink():
        raise KernelError("WRITE_TARGET_INVALID", "write_target must not be a symbolic link")
    target_path = raw_target.resolve()
    if not is_within(ledger_dir, target_path):
        raise KernelError("WRITE_TARGET_INVALID", "write_target escapes the ledger directory")
    if not target_path.is_file():
        raise KernelError("WRITE_TARGET_INVALID", f"write_target is missing: {write_target}")
    target_info = read_text_file(target_path)
    newline = target_info.newline

    rendered = render_fragment(parsed.transactions, newline)
    if fault_hook is not None:
        override = fault_hook("render_fragment", {"fragment": rendered})
        if isinstance(override, str):
            rendered = override
    rendered_sha256 = "sha256:" + sha256_bytes(rendered.encode("utf-8"))
    fragment = fragment_for(target_info.text, rendered, newline)
    fragment_sha256 = "sha256:" + sha256_bytes(fragment.encode("utf-8"))
    new_text = target_info.text + fragment
    new_bytes = encode_text_file(new_text, target_info)

    review_checks = _evidence_checks(ledger_dir, parsed)
    _duplicate_checks(review_checks, parsed, baseline)

    if baseline.errors:
        _check(
            checks,
            "BASELINE_INVALID",
            "block",
            f"the current ledger has {len(baseline.errors)} Beancount error(s); controlled commits are blocked",
            details={"errors": baseline.error_records(baseline.errors)[:20]},
        )
        review_checks.extend(checks)
        status = "blocked"
        return DryRunResult(
            status=status,
            checks=review_checks,
            diff={"changed_files": [], "postings": [], "balances_before": [], "balances_after": [],
                  "file_diff": "", "diff_unavailable": True},
            fragment=fragment,
            fragment_sha256=fragment_sha256,
            review_digest=review_digest(rendered_sha256, list(parsed.accounts), review_checks),
            candidate=None,
        )

    temp_root = Path(tempfile.mkdtemp(prefix="ledger-kernel-dryrun-"))
    try:
        _copy_closure(baseline, temp_root)
        temp_target = temp_root / write_target
        temp_target.parent.mkdir(parents=True, exist_ok=True)
        temp_target.write_bytes(new_bytes)
        temp_adapter = adapter_factory(temp_root)
        candidate = temp_adapter.load(temp_root / baseline.root_file.relative_to(baseline.ledger_dir))

        for error in candidate.error_records(candidate.errors):
            _check(
                checks,
                "BEANCOUNT_ERROR",
                "block",
                f"{error['type']}: {error['message']}",
                details={"source_file": error.get("source_file"), "lineno": error.get("lineno")},
            )

        expected = [canonical_transaction(txn) for txn in parsed.transactions]
        delta_ok, delta_details = _compare_delta(baseline, candidate, expected)
        if not delta_ok:
            _check(
                checks,
                "DELTA_MISMATCH",
                "block",
                f"the loaded candidate is not exactly the proposal: {delta_details['reason']}",
                details=delta_details,
            )

        option_changes = _check_options(baseline, candidate)
        if option_changes:
            _check(
                checks,
                "OPTIONS_CHANGED",
                "block",
                "candidate loading changed Beancount options: " + ", ".join(option_changes),
                details={"changed": option_changes},
            )

        other_changes = _check_other_files(baseline, candidate, write_target)
        if other_changes:
            _check(
                checks,
                "DELTA_MISMATCH",
                "block",
                "candidate loading changed files other than write_target",
                details={"changed_files": other_changes},
            )

        for transaction in parsed.transactions:
            for posting in transaction.postings:
                info = baseline.accounts.get(posting.account)
                if info is None:
                    _check(
                        checks,
                        "ACCOUNT_NOT_OPEN",
                        "block",
                        f"account is not open in the current ledger: {posting.account}",
                        account=posting.account,
                    )
                elif not info.is_open_on(transaction.date):
                    _check(
                        checks,
                        "ACCOUNT_NOT_OPEN",
                        "block",
                        f"account is not open on {transaction.date.isoformat()}: {posting.account}",
                        account=posting.account,
                    )

        if parsed.confidence is not None and parsed.confidence < 0.5:
            _check(
                review_checks,
                "LOW_CONFIDENCE",
                "warning",
                f"proposal confidence is low ({parsed.confidence:.2f}); it is not an approval signal",
            )

        balances_before, balances_after, deltas = _balance_diff(BeancountAdapter(ledger_dir), baseline, candidate, parsed)
        balance_map = {
            (row["account"], row["currency"]): Decimal(row["amount"]) for row in balances_after
        }
        review_checks.extend(
            evaluate_contract(
                contract,
                parsed,
                agent_id=agent_id,
                load=baseline,
                balances_after=balance_map,
                reconciliation_status=reconciliation_status,
            )
        )
        checks.extend(review_checks)

        status = "blocked" if any(c["severity"] == "block" for c in checks) else (
            "pass_with_warnings" if any(c["severity"] == "warning" for c in checks) else "pass"
        )
        diff = {
            "changed_files": [write_target],
            "postings": [
                {
                    "date": transaction.date.isoformat(),
                    "payee": transaction.payee,
                    "narration": transaction.narration,
                    "account": posting.account,
                    "units": format(posting.units, "f"),
                    "currency": posting.currency,
                }
                for transaction in parsed.transactions
                for posting in transaction.postings
            ],
            "balances_before": balances_before,
            "balances_after": balances_after,
            "deltas": deltas,
            "file_diff": _file_diff(write_target, target_info.text, new_text),
            "diff_unavailable": False,
            "fragment_preview": "\n".join(
                escape_display(line) for line in fragment.splitlines()[:80]
            ),
        }
        return DryRunResult(
            status=status,
            checks=checks,
            diff=diff,
            fragment=fragment,
            fragment_sha256=fragment_sha256,
            review_digest=review_digest(rendered_sha256, list(parsed.accounts), checks),
            candidate=candidate,
        )
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def verify_fragment(fragment: str, expected_sha: str) -> None:
    actual = "sha256:" + sha256_bytes(fragment.encode("utf-8"))
    if actual != expected_sha:
        raise KernelError(
            FRAGMENT_MISMATCH,
            "the rendered fragment no longer matches the validated fragment",
            {"expected": expected_sha, "actual": actual},
        )


def verify_candidate(
    *,
    candidate: LoadResult,
    baseline: LoadResult,
    parsed: ParsedDraft,
    write_target: str,
) -> tuple[bool, list[dict[str, Any]]]:
    """Second, commit-time delta check on current files."""
    problems: list[dict[str, Any]] = []
    if candidate.errors:
        problems.append(
            {
                "code": "BEANCOUNT_ERROR",
                "severity": "block",
                "message": f"candidate has {len(candidate.errors)} Beancount error(s)",
                "errors": candidate.error_records(candidate.errors)[:20],
            }
        )
    expected = [canonical_transaction(txn) for txn in parsed.transactions]
    delta_ok, details = _compare_delta(baseline, candidate, expected)
    if not delta_ok:
        problems.append({"code": DELTA_MISMATCH, "severity": "block", "message": details["reason"], "details": details})
    option_changes = _check_options(baseline, candidate)
    if option_changes:
        problems.append(
            {
                "code": "OPTIONS_CHANGED",
                "severity": "block",
                "message": "options changed: " + ", ".join(option_changes),
            }
        )
    other_changes = _check_other_files(baseline, candidate, write_target)
    if other_changes:
        problems.append(
            {
                "code": DELTA_MISMATCH,
                "severity": "block",
                "message": "files other than write_target changed",
                "details": {"changed_files": other_changes},
            }
        )
    return not problems, problems
