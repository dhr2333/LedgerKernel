#!/usr/bin/env python3
"""Account-layer dogfood baseline measured on a disposable ledger copy.

By default this runs against the synthetic fixture in tests/fixtures/ledger so
anybody can reproduce the numbers (and CI/agents can compare runs) with no
private data:

    PYTHONPATH=src python3 scripts/dogfood_account_layer.py

To measure a real ledger, point --source at a directory holding the root
main.bean. Everything happens in a temporary copy; the source directory is only
ever read:

    PYTHONPATH=src python3 scripts/dogfood_account_layer.py --source <TestAssets> --json

Measured flow (v0.2.x account layer; writes are kernel metadata only, the ledger
file is never touched):

1. init, read accounts, select the ones active today (lifecycle == open);
2. generate a contracts.yaml covering every active account (monthly
   reconciliation cadence for Assets:/Liabilities:), then apply it through the
   public apply_contracts dual-hash CAS path;
3. reconcile every active account that has a cadence against today's ledger
   balance (declared == ledger balance, so the recorded difference is zero);
4. submit and validate three drafts (never approved or committed) so the review
   queue is non-empty, then time 'review --by-account' three times - the W3
   grouped view is exercised through the real CLI, not a Python shortcut;
5. time one full ledger load and one audit-chain read so the cost of a single
   metadata operation is attributable.

Checked invariants: the source directory is byte-identical afterwards, the
write target bytes are unchanged (no Ledger write), ledger_revision is identical
after every metadata operation, the audit chain verifies, contract coverage is
100%, every reconciliation is recorded, no observation is created and no draft
is committed.

The JSON report contains counts, timings and status distributions only; it never
emits real account names, payees or amounts (account prefixes are aggregated).
Exit code 0 means every check passed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

import beancount

from ledger_kernel import __version__
from ledger_kernel.errors import KernelError
from ledger_kernel.kernel import Kernel
from ledger_kernel.revision import contract_revision_bytes

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "ledger"
SOURCE_TREE = REPO_ROOT / "src"

NL = chr(10)
TARGET = "transactions/kernel-import.bean"
TARGET_HEADER = "; LedgerKernel write_target" + NL
COPY_IGNORE = (".git", ".ledger-kernel", ".stversions", ".stfolder", "__pycache__", "*.pyc")
SNAPSHOT_SKIP_DIRS = {".git", ".ledger-kernel", ".stversions", ".stfolder", "__pycache__"}

CADENCE_PREFIXES = ("Assets:", "Liabilities:")
CADENCE = "monthly"
DRAFT_AMOUNTS = ("12.34", "23.45", "34.56")
DRAFT_AGENT = "dogfood-agent"
CLI_RUNS = 3


# --------------------------------------------------------------------------- utils
def _timed(func: Any, /, *args: Any, **kwargs: Any) -> tuple[Any, float]:
    """Call func and return (result, milliseconds)."""
    start = time.perf_counter()
    value = func(*args, **kwargs)
    return value, round((time.perf_counter() - start) * 1000.0, 3)


def _percentile(ordered: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile of an already sorted sequence."""
    index = max(0, math.ceil(fraction * len(ordered)) - 1)
    return ordered[index]


def _stats(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {
            "count": 0,
            "total_ms": 0.0,
            "mean_ms": None,
            "p50_ms": None,
            "p95_ms": None,
            "min_ms": None,
            "max_ms": None,
        }
    ordered = sorted(float(value) for value in values)
    return {
        "count": len(ordered),
        "total_ms": round(sum(ordered), 3),
        "mean_ms": round(sum(ordered) / len(ordered), 3),
        "p50_ms": round(_percentile(ordered, 0.50), 3),
        "p95_ms": round(_percentile(ordered, 0.95), 3),
        "min_ms": round(ordered[0], 3),
        "max_ms": round(ordered[-1], 3),
    }


def _median(values: Sequence[float]) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[middle], 3)
    return round((ordered[middle - 1] + ordered[middle]) / 2.0, 3)


def _growth_ratio(values: Sequence[float], window: int = 10) -> float | None:
    """First-window mean vs last-window mean; > 1 means later ops were slower."""
    if len(values) < window * 2:
        return None
    head = sum(float(value) for value in values[:window]) / window
    tail = sum(float(value) for value in values[-window:]) / window
    if not head:
        return None
    return round(tail / head, 3)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _snapshot_tree(root: Path) -> dict[str, tuple[int, int, str]]:
    """Map relative path -> (size, mtime_ns, sha256) for every source file."""
    snapshot: dict[str, tuple[int, int, str]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in SNAPSHOT_SKIP_DIRS)
        for filename in sorted(filenames):
            if filename.endswith(".pyc"):
                continue
            path = Path(dirpath) / filename
            try:
                info = path.stat()
                digest = _sha256_file(path)
            except OSError:
                continue
            snapshot[path.relative_to(root).as_posix()] = (info.st_size, info.st_mtime_ns, digest)
    return snapshot


def _snapshot_diff(
    before: dict[str, tuple[int, int, str]], after: dict[str, tuple[int, int, str]]
) -> dict[str, int]:
    """Counts only: the report must not carry source file names."""
    return {
        "added": len(set(after) - set(before)),
        "removed": len(set(before) - set(after)),
        "changed": sum(1 for name in set(before) & set(after) if before[name] != after[name]),
    }


def _check(checks: list[dict[str, Any]], name: str, ok: bool, detail: Any = None) -> None:
    checks.append({"check": name, "ok": bool(ok), "detail": detail})


# ---------------------------------------------------------------------- ledger copy
def _prepare_copy(source: Path, workdir: Path) -> tuple[Path, Path, Path]:
    """Copy the source into workdir/ledger and make TARGET writable and included."""
    ledger = workdir / "ledger"
    shutil.copytree(source, ledger, ignore=shutil.ignore_patterns(*COPY_IGNORE))
    target = ledger / TARGET
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_text(TARGET_HEADER, encoding="utf-8")
    main_bean = ledger / "main.bean"
    text = main_bean.read_text(encoding="utf-8")
    include = 'include "' + TARGET + '"'
    if include not in text:
        main_bean.write_text(text.rstrip(NL) + NL + NL + include + NL, encoding="utf-8")
    return ledger, main_bean, target


# ------------------------------------------------------------------- contract YAML
def _contracts_yaml(active_accounts: Sequence[str]) -> str:
    """A contracts file that manages every active account, without real data."""
    lines = [
        "# Generated by scripts/dogfood_account_layer.py on a disposable ledger copy.",
        "schema_version: 1",
        "unmanaged_accounts: []",
        "accounts:",
    ]
    for name in sorted(active_accounts):
        quoted = json.dumps(name, ensure_ascii=False)
        if name.startswith(CADENCE_PREFIXES):
            lines.extend(
                [
                    "  " + quoted + ":",
                    "    policy:",
                    "      reconciliation:",
                    "        cadence: " + CADENCE,
                ]
            )
        else:
            lines.append("  " + quoted + ": {}")
    return NL.join(lines) + NL


def _pick_draft_pair(active_rows: Sequence[dict[str, Any]]) -> tuple[str, str, str]:
    """Pick an Expenses:/Assets: pair that shares a currency (synthetic drafts only)."""
    currency_counts: Counter = Counter()
    for row in active_rows:
        currency_counts.update((row.get("balances") or {}).keys())
    for currency, _count in currency_counts.most_common():
        expenses = sorted(
            row["account"]
            for row in active_rows
            if row["account"].startswith("Expenses:") and currency in (row.get("balances") or {})
        )
        assets = sorted(
            row["account"]
            for row in active_rows
            if row["account"].startswith("Assets:") and currency in (row.get("balances") or {})
        )
        if expenses and assets:
            return expenses[0], assets[0], currency
    raise SystemExit("could not find an active Expenses:/Assets: pair with a shared currency")


def _draft_payload(day: dt.date, expense: str, asset: str, currency: str, index: int) -> dict[str, Any]:
    amount = DRAFT_AMOUNTS[index]
    return {
        "schema_version": 1,
        "operation": "append_transactions",
        "transactions": [
            {
                "date": day.isoformat(),
                "flag": "*",
                "payee": "Dogfood Shop",
                "narration": "account-layer dogfood draft " + str(index + 1),
                "postings": [
                    {"account": expense, "units": amount, "currency": currency},
                    {"account": asset, "units": "-" + amount, "currency": currency},
                ],
            }
        ],
        "confidence": 0.99,
        "rationale_summary": "synthetic draft for review-queue measurement; never approved or committed",
    }


# ------------------------------------------------------------------------- CLI runs
def _run_cli(command: Sequence[str], timeout: float = 600.0) -> tuple[float, subprocess.CompletedProcess[str]]:
    env = os.environ.copy()
    python_path = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(SOURCE_TREE) + (os.pathsep + python_path if python_path else "")
    start = time.perf_counter()
    proc = subprocess.run(
        list(command),
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return round((time.perf_counter() - start) * 1000.0, 3), proc


def _cli_command(ledger: Path, extra: Sequence[str]) -> list[str]:
    return [sys.executable, "-m", "ledger_kernel", "--ledger", str(ledger), "--json", *extra]


def _cli_startup() -> dict[str, Any]:
    times: list[float] = []
    for _ in range(CLI_RUNS):
        elapsed, proc = _run_cli([sys.executable, "-m", "ledger_kernel", "--version"])
        if proc.returncode == 0:
            times.append(elapsed)
    return {"runs": CLI_RUNS, "times_ms": times, "median_ms": _median(times), "ok": len(times) == CLI_RUNS}


def _review_runs(ledger: Path, extra: Sequence[str]) -> dict[str, Any]:
    times: list[float] = []
    payloads: list[dict[str, Any]] = []
    failures = 0
    for _ in range(CLI_RUNS):
        elapsed, proc = _run_cli(_cli_command(ledger, extra))
        if proc.returncode != 0:
            failures += 1
            continue
        try:
            payloads.append(json.loads(proc.stdout))
        except json.JSONDecodeError:
            failures += 1
            continue
        times.append(elapsed)
    if not times:
        return {"ok": False, "runs": CLI_RUNS, "failures": failures}
    payload = payloads[-1]
    result: dict[str, Any] = {
        "ok": True,
        "runs": CLI_RUNS,
        "failures": failures,
        "times_ms": times,
        "median_ms": _median(times),
    }
    if payload.get("mode") == "by_account":
        groups = payload.get("groups") or []
        result["groups"] = len(groups)
        result["draft_links"] = sum(int(group.get("draft_count") or 0) for group in groups)
        result["contract_warnings"] = sum(len(group.get("warnings") or []) for group in groups)
        result["accounts_with_pending"] = sum(1 for group in groups if group.get("draft_count"))
    else:
        result["drafts"] = len(payload.get("drafts") or [])
    return result


# ---------------------------------------------------------------------- measurement
def _measure(args: argparse.Namespace, source: Path, workdir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    today = dt.date.today()
    checks: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
        "as_of": today.isoformat(),
        "environment": {
            "python": platform.python_version(),
            "beancount": beancount.__version__,
            "ledger_kernel": __version__,
        },
    }

    using_fixture = source == FIXTURE.resolve()
    source_before = _snapshot_tree(source)
    report["source"] = {
        "kind": "fixture" if using_fixture else "external",
        "path": str(source),
        "files": len(source_before),
        "bytes": sum(size for size, _mtime, _digest in source_before.values()),
    }

    ledger, main_bean, target_path = _prepare_copy(source, workdir)
    kernel, init_ms = _timed(Kernel.init, main_bean, TARGET)
    status, first_read_ms = _timed(kernel.get_status)

    target_sha_before = _sha256_file(target_path)
    revisions: dict[str, str] = {"after_init": status["ledger_revision"]}

    rows, list_accounts_ms = _timed(kernel.list_accounts, today)
    active = [row for row in rows if row.get("lifecycle") == "open"]
    lifecycle_counts = Counter(row.get("lifecycle") for row in rows)
    prefix_counts = Counter(row["account"].split(":", 1)[0] for row in active)
    report["init"] = {
        "status": status["status"],
        "load_ok": status["ledger_load_ok"],
        "beancount_error_count": status["beancount_error_count"],
        "write_target_in_closure": status["write_target_in_closure"],
        "files_in_closure": status["files_in_closure"],
        "accounts_total": len(rows),
        "active": len(active),
        "lifecycle_counts": {str(key): count for key, count in sorted(lifecycle_counts.items(), key=lambda item: str(item[0]))},
        "active_prefix_counts": dict(sorted(prefix_counts.items())),
        "init_ms": init_ms,
        "first_read_ms": first_read_ms,
        "list_accounts_ms": list_accounts_ms,
    }
    _check(
        checks,
        "init loads ledger cleanly",
        bool(status["ledger_load_ok"]) and status["status"] == "Clean",
        {"status": status["status"], "beancount_errors": status["beancount_error_count"]},
    )
    _check(checks, "write_target is part of the include closure", bool(status["write_target_in_closure"]))
    _check(checks, "active accounts discovered", bool(active), {"accounts_total": len(rows), "active": len(active)})
    if not status["ledger_load_ok"] or not active:
        return report, checks

    # Cost attribution: every public call reloads the whole ledger; measure one
    # reload and one audit-chain read so per-operation cost is explainable.
    cost_profile: dict[str, Any] = {}
    try:
        _load, load_ms = _timed(kernel.adapter.load, kernel.config.root_path)
        cost_profile["full_ledger_load_ms"] = load_ms
    except Exception as exc:  # pragma: no cover - instrumentation only
        cost_profile["full_ledger_load_ms"] = None
        cost_profile["full_ledger_load_error"] = type(exc).__name__
    try:
        events, audit_read_ms = _timed(kernel.store.audit_events)
        cost_profile["audit_events_read_ms"] = audit_read_ms
        cost_profile["audit_events_at_start"] = len(events)
    except Exception as exc:  # pragma: no cover - instrumentation only
        cost_profile["audit_events_read_ms"] = None
        cost_profile["audit_events_at_start"] = None
        cost_profile["audit_events_read_error"] = type(exc).__name__
    report["cost_profile"] = cost_profile

    # ---------------------------------------------------------- contracts apply
    view_before, contracts_view_ms = _timed(kernel.contracts_view)
    candidate_text = _contracts_yaml([row["account"] for row in active])
    confirm = contract_revision_bytes(candidate_text.encode("utf-8"))
    apply_result, apply_ms = _timed(
        kernel.apply_contracts,
        candidate_text,
        expected_current=view_before["contract_revision"],
        confirm=confirm,
        note="dogfood baseline: one policy entry per active account",
    )
    status_after_contracts, _ = _timed(kernel.get_status)
    revisions["after_contracts"] = status_after_contracts["ledger_revision"]
    view_after, coverage_view_ms = _timed(kernel.contracts_view)
    coverage = view_after["coverage"]
    cadence_accounts = [
        row["account"] for row in active if row["account"].startswith(CADENCE_PREFIXES)
    ]
    report["contracts"] = {
        "applied": bool(apply_result.get("applied")),
        "declared_accounts": len(view_after.get("accounts") or {}),
        "unmanaged_accounts": len(view_after.get("unmanaged_accounts") or []),
        "active_accounts": coverage.get("active_account_count"),
        "managed_accounts": len(coverage.get("managed_accounts") or []),
        "uncovered_accounts": len(coverage.get("uncovered_accounts") or []),
        "coverage_ratio": coverage.get("ratio"),
        "cadence_accounts": len(cadence_accounts),
        "warnings": len(view_after.get("warnings") or []),
        "contract_revision": view_after.get("contract_revision"),
        "previous_revision": apply_result.get("previous_revision"),
        "contracts_view_ms": contracts_view_ms,
        "apply_ms": apply_ms,
        "coverage_view_ms": coverage_view_ms,
    }
    _check(checks, "contracts applied through dual-hash CAS", bool(apply_result.get("applied")))
    _check(
        checks,
        "contract coverage is 100% of active accounts",
        not coverage.get("uncovered_accounts")
        and len(coverage.get("managed_accounts") or []) == coverage.get("active_account_count"),
        {
            "active": coverage.get("active_account_count"),
            "managed": len(coverage.get("managed_accounts") or []),
            "uncovered": len(coverage.get("uncovered_accounts") or []),
        },
    )

    # -------------------------------------------------------------- reconcile
    reconcile_durations: list[float] = []
    recorded_statuses: Counter = Counter()
    error_codes: list[str] = []
    for row in active:
        if not row["account"].startswith(CADENCE_PREFIXES):
            continue
        declared = {currency: Decimal(value) for currency, value in (row.get("balances") or {}).items()}
        try:
            result, elapsed = _timed(
                kernel.reconcile_account,
                row["account"],
                as_of=today,
                declared=declared or None,
            )
        except KernelError as exc:
            error_codes.append(exc.code)
            continue
        reconcile_durations.append(elapsed)
        recorded_statuses[result["reconciliation"]["status"]] += 1
    status_after_reconcile, _ = _timed(kernel.get_status)
    revisions["after_reconcile"] = status_after_reconcile["ledger_revision"]

    report["reconcile"] = {
        "attempted": len(cadence_accounts),
        "recorded": len(reconcile_durations),
        "failed": len(error_codes),
        "error_codes": sorted(set(error_codes)),
        "status_from_records": dict(sorted(recorded_statuses.items())),
        "durations_ms": reconcile_durations,
        **_stats(reconcile_durations),
        "first10_mean_ms": round(sum(reconcile_durations[:10]) / 10, 3) if len(reconcile_durations) >= 10 else None,
        "last10_mean_ms": round(sum(reconcile_durations[-10:]) / 10, 3) if len(reconcile_durations) >= 10 else None,
        "growth_ratio": _growth_ratio(reconcile_durations),
        "audit_events_after": status_after_reconcile["audit_integrity"]["event_count"],
    }
    _check(
        checks,
        "every cadence account was reconciled",
        len(reconcile_durations) == len(cadence_accounts) and not error_codes,
        {
            "cadence_accounts": len(cadence_accounts),
            "recorded": len(reconcile_durations),
            "error_codes": sorted(set(error_codes)),
        },
    )

    # ------------------------------------------------- account views + status mix
    account_view_durations: list[float] = []
    view_statuses: Counter = Counter()
    view_errors: list[str] = []
    for row in active:
        try:
            account, elapsed = _timed(kernel.get_account, row["account"])
        except KernelError as exc:
            view_errors.append(exc.code)
            continue
        account_view_durations.append(elapsed)
        view_statuses[account["reconciliation"]["status"]] += 1
    report["account_view"] = {
        "sampled": len(account_view_durations),
        "failed": len(view_errors),
        "error_codes": sorted(set(view_errors)),
        "durations_ms": account_view_durations,
        **_stats(account_view_durations),
    }
    report["reconciliation_status"] = {
        "source": "get_account",
        "active_accounts": len(account_view_durations),
        "cadence_accounts": len(cadence_accounts),
        "current": view_statuses.get("current", 0),
        "overdue": view_statuses.get("overdue", 0),
        "never": view_statuses.get("never", 0),
        "not_configured": view_statuses.get("not_configured", 0),
    }
    _check(
        checks,
        "no cadence account is overdue or never reconciled",
        view_statuses.get("overdue", 0) == 0 and view_statuses.get("never", 0) == 0,
        dict(report["reconciliation_status"]),
    )
    _check(
        checks,
        "reconciliation status mix is consistent with contracts",
        view_statuses.get("current", 0) == len(cadence_accounts)
        and view_statuses.get("not_configured", 0) == len(active) - len(cadence_accounts),
        {"current": view_statuses.get("current", 0), "not_configured": view_statuses.get("not_configured", 0)},
    )

    # ------------------------------------------------------------------ drafts
    expense, asset, currency = _pick_draft_pair(active)
    submit_durations: list[float] = []
    draft_ids: list[str] = []
    for index, _amount in enumerate(DRAFT_AMOUNTS):
        payload = _draft_payload(today, expense, asset, currency, index)
        entry, elapsed = _timed(
            kernel.submit_draft,
            payload,
            "dogfood-account-layer-draft-" + str(index + 1),
            agent_id=DRAFT_AGENT,
        )
        submit_durations.append(elapsed)
        draft_ids.append(entry["draft"]["draft_id"])

    validate_durations: list[float] = []
    validation_statuses: Counter = Counter()
    validation_codes: Counter = Counter()
    for draft_id in draft_ids:
        validation, elapsed = _timed(kernel.validate_draft, draft_id)
        validate_durations.append(elapsed)
        validation_statuses[validation["status"]] += 1
        for check in validation.get("checks") or []:
            validation_codes[str(check.get("code"))] += 1

    draft_rows, list_drafts_ms = _timed(kernel.list_drafts)
    draft_states = Counter(row.get("state") for row in draft_rows)
    report["drafts"] = {
        "submitted": len(submit_durations),
        "validated": len(validate_durations),
        "submit_ms": _stats(submit_durations),
        "validate_ms": _stats(validate_durations),
        "validate_p50_ms": _stats(validate_durations)["p50_ms"],
        "validate_total_ms": _stats(validate_durations)["total_ms"],
        "validation_statuses": dict(sorted(validation_statuses.items())),
        "check_codes": dict(sorted(validation_codes.items())),
        "states": {
            str(state): count
            for state, count in sorted(draft_states.items(), key=lambda item: str(item[0]))
        },
        "list_drafts_ms": list_drafts_ms,
    }
    pending_states = {"Submitted", "NeedsHumanApproval", "Blocked", "Approved"}
    _check(
        checks,
        "three drafts validated and left pending (never approved/committed)",
        len(validate_durations) == len(DRAFT_AMOUNTS)
        and all(state in pending_states for state in draft_states)
        and "Committed" not in draft_states,
        {
            "validated": len(validate_durations),
            "validation_statuses": dict(validation_statuses),
            "states": dict(draft_states),
        },
    )

    # ------------------------------------------------------ review CLI timings
    startup = _cli_startup()
    by_account = _review_runs(ledger, ["review", "--by-account"])
    queue = _review_runs(ledger, ["review"])
    net_by_account = None
    if by_account.get("median_ms") is not None and startup.get("median_ms") is not None:
        net_by_account = round(max(by_account["median_ms"] - startup["median_ms"], 0.0), 3)
    net_queue = None
    if queue.get("median_ms") is not None and startup.get("median_ms") is not None:
        net_queue = round(max(queue["median_ms"] - startup["median_ms"], 0.0), 3)
    report["review"] = {
        "cli_startup": startup,
        "by_account": {**by_account, "net_ms": net_by_account},
        "queue": {**queue, "net_ms": net_queue},
    }
    _check(
        checks,
        "review --by-account succeeded and grouped the pending drafts",
        bool(by_account.get("ok")) and int(by_account.get("groups") or 0) > 0,
        {
            "groups": by_account.get("groups"),
            "draft_links": by_account.get("draft_links"),
            "failures": by_account.get("failures"),
        },
    )
    _check(
        checks,
        "review queue succeeded",
        bool(queue.get("ok")),
        {"drafts": queue.get("drafts"), "failures": queue.get("failures")},
    )

    # -------------------------------------------------------------- final state
    status_final, final_status_ms = _timed(kernel.get_status)
    revisions["final"] = status_final["ledger_revision"]
    target_sha_after = _sha256_file(target_path)
    source_after = _snapshot_tree(source)
    source_diff = _snapshot_diff(source_before, source_after)

    try:
        records, records_read_ms = _timed(kernel._reconciliation_records_from_audit)
        report["cost_profile"]["reconciliation_records_read_ms"] = records_read_ms
        report["cost_profile"]["reconciliation_records_count"] = len(records)
    except Exception as exc:  # pragma: no cover - instrumentation only
        report["cost_profile"]["reconciliation_records_read_ms"] = None
        report["cost_profile"]["reconciliation_records_error"] = type(exc).__name__

    report["ledger"] = {
        "write_target": TARGET,
        "ledger_revision": status_final["ledger_revision"],
        "files_in_closure": status_final["files_in_closure"],
        "audit_events": status_final["audit_integrity"]["event_count"],
        "audit_ok": status_final["audit_integrity"]["ok"],
        "draft_counts": {
            str(state): count for state, count in (status_final.get("draft_counts") or {}).items()
        },
        "observations": status_final["observations"],
        "final_status_ms": final_status_ms,
    }
    report["invariants"] = {
        "source_unchanged": not any(source_diff.values()),
        "source_changes": source_diff,
        "ledger_revision_stable": len(set(revisions.values())) == 1,
        "distinct_ledger_revisions": len(set(revisions.values())),
        "write_target_unchanged": target_sha_before == target_sha_after,
        "audit_ok": status_final["audit_integrity"]["ok"],
        "observations": status_final["observations"],
        "no_drafts_committed": not any(state == "Committed" for state in draft_states),
    }

    _check(checks, "source directory is byte-identical after the run", not any(source_diff.values()), source_diff)
    _check(
        checks,
        "ledger_revision unchanged across all metadata operations",
        len(set(revisions.values())) == 1,
        {"distinct_revisions": len(set(revisions.values()))},
    )
    _check(checks, "write_target bytes unchanged (no Ledger write)", target_sha_before == target_sha_after)
    _check(
        checks,
        "audit chain verifies",
        bool(status_final["audit_integrity"]["ok"]),
        {"event_count": status_final["audit_integrity"]["event_count"]},
    )
    _check(checks, "no external-change observation was created", status_final["observations"] == 0)
    _check(checks, "no draft was committed", not any(state == "Committed" for state in draft_states))
    return report, checks


def _summarize(report: dict[str, Any], checks: Sequence[dict[str, Any]]) -> dict[str, Any]:
    contracts = report.get("contracts") or {}
    reconcile = report.get("reconcile") or {}
    account_view = report.get("account_view") or {}
    drafts = report.get("drafts") or {}
    review = report.get("review") or {}
    by_account = review.get("by_account") or {}
    queue = review.get("queue") or {}
    startup = review.get("cli_startup") or {}
    failed = [item for item in checks if not item["ok"]]
    return {
        "active_accounts": (report.get("init") or {}).get("active"),
        "cadence_accounts": contracts.get("cadence_accounts"),
        "contract_coverage_ratio": contracts.get("coverage_ratio"),
        "reconciliations_recorded": reconcile.get("recorded"),
        "reconciliations_failed": reconcile.get("failed"),
        "reconcile_p50_ms": reconcile.get("p50_ms"),
        "reconcile_p95_ms": reconcile.get("p95_ms"),
        "reconcile_max_ms": reconcile.get("max_ms"),
        "reconcile_total_ms": reconcile.get("total_ms"),
        "reconcile_first10_mean_ms": reconcile.get("first10_mean_ms"),
        "reconcile_last10_mean_ms": reconcile.get("last10_mean_ms"),
        "reconcile_growth_ratio": reconcile.get("growth_ratio"),
        "account_view_p50_ms": account_view.get("p50_ms"),
        "account_view_p95_ms": account_view.get("p95_ms"),
        "account_view_total_ms": account_view.get("total_ms"),
        "validate_p50_ms": drafts.get("validate_p50_ms"),
        "validate_total_ms": drafts.get("validate_total_ms"),
        "review_by_account_median_ms": by_account.get("median_ms"),
        "review_by_account_net_ms": by_account.get("net_ms"),
        "review_queue_median_ms": queue.get("median_ms"),
        "review_queue_net_ms": queue.get("net_ms"),
        "cli_startup_median_ms": startup.get("median_ms"),
        "audit_events": (report.get("ledger") or {}).get("audit_events"),
        "checks_passed": len(checks) - len(failed),
        "checks_failed": len(failed),
        "checks_total": len(checks),
    }


# ---------------------------------------------------------------------------- CLI
def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--source",
        type=Path,
        default=None,
        help="ledger directory containing main.bean (default: synthetic fixture)",
    )
    parser.add_argument("--keep", action="store_true", help="keep the temporary ledger copy")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    source = (args.source or FIXTURE).expanduser().resolve()
    if not (source / "main.bean").is_file():
        raise SystemExit(f"no main.bean under {source}")

    workdir = Path(tempfile.mkdtemp(prefix="ledger-kernel-dogfood-"))
    try:
        report, checks = _measure(args, source, workdir)
    finally:
        if args.keep:
            print(f"kept: {workdir}", file=sys.stderr)
        else:
            shutil.rmtree(workdir, ignore_errors=True)

    failed = [item for item in checks if not item["ok"]]
    report["checks"] = checks
    report["failed"] = failed
    report["metrics"] = _summarize(report, checks)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        for item in checks:
            print(("PASS " if item["ok"] else "FAIL ") + item["check"])
        print(json.dumps(report["metrics"], ensure_ascii=False, indent=2, default=str))
        if failed:
            print(f"{len(failed)} check(s) failed", file=sys.stderr)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
