"""Kernel use cases: status, account reads, drafts, approval and controlled commit.

The public surface here is what CLI and MCP adapters call. Neither adapter is
allowed to re-implement validation or write logic.
"""

from __future__ import annotations

import datetime as dt
import difflib
import json
import os
import re
import shutil
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import __version__
from .adapter import AccountInfo, BeancountAdapter, LoadResult
from .config import Config
from .contracts import (
    Contract,
    account_contract_view,
    contract_coverage,
    contract_warnings,
    load_contract,
    parse_contract_text,
)
from .dryrun import FaultHook, copy_ledger_workspace, dry_run, fragment_for, verify_candidate, verify_fragment
from .errors import (
    APPROVAL_EXPIRED,
    APPROVAL_REQUIRED,
    AUDIT_INTEGRITY,
    CONTRACT_CONFLICT,
    CONTRACT_INVALID,
    DELTA_MISMATCH,
    EXTERNAL_CHANGE,
    INVALID_STATE,
    LEDGER_INVALID,
    LEDGER_LOAD_FAILED,
    RECOVERY_REQUIRED,
    SCHEMA_INVALID,
    STALE_REVISION,
    WRITE_TARGET_INVALID,
    KernelError,
)
from .locks import FileLock
from .revision import (
    contract_revision,
    contract_revision_bytes,
    diff_manifests,
    engine_fingerprint,
    revision_of,
    revision_short,
)
from .schema import ParsedDraft, parse_payload, render_fragment
from .store import Store
from .util import (
    atomic_write_bytes,
    canonical_json_bytes,
    ensure_dir,
    encode_text_file,
    escape_display,
    fsync_dir,
    is_within,
    new_id,
    now_iso,
    parse_datetime,
    read_text_file,
    sha256_bytes,
    sha256_file,
    truncate,
)

TERMINAL_DRAFT_STATES = {"Committed", "Rejected", "Withdrawn", "Expired", "Superseded"}
ACTIVE_DRAFT_STATES = {"Submitted", "NeedsHumanApproval", "Approved", "Blocked", "Validating"}


def _utcnow() -> dt.datetime:
    return dt.datetime.now(tz=dt.timezone.utc)


_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


def _canonical_contract_hash(value: Any) -> str | None:
    """Normalize a contract hash argument; None means missing or malformed.

    Accepted forms: "absent" / "sha256:absent", "sha256:<64 hex>", and bare
    "<64 hex>". Anything else is rejected as a conflict, never coerced.
    """
    if value is None or not isinstance(value, str):
        return None
    text = value.strip().lower()
    if not text:
        return None
    if text in {"absent", "sha256:absent"}:
        return "sha256:absent"
    if text.startswith("sha256:"):
        text = text.split(":", 1)[1]
    return f"sha256:{text}" if _HEX64_RE.match(text) else None


def _affected_contracts(current: Contract, new: Contract) -> dict[str, Any]:
    current_names = set(current.accounts)
    new_names = set(new.accounts)
    modified = sorted(
        name
        for name in current_names & new_names
        if account_contract_view(current.accounts[name]) != account_contract_view(new.accounts[name])
    )
    old_unmanaged = set(current.unmanaged_accounts)
    new_unmanaged = set(new.unmanaged_accounts)
    return {
        "accounts": {
            "added": sorted(new_names - current_names),
            "removed": sorted(current_names - new_names),
            "modified": modified,
        },
        "unmanaged": {
            "added": sorted(new_unmanaged - old_unmanaged),
            "removed": sorted(old_unmanaged - new_unmanaged),
        },
    }


class Kernel:
    def __init__(self, config: Config, store: Store, fault_hook: FaultHook | None = None):
        self.config = config
        self.store = store
        self.adapter = BeancountAdapter(config.ledger_dir)
        self.fault_hook = fault_hook
        self._load: LoadResult | None = None
        self._state: dict[str, Any] | None = None
        self._contract: Contract | None = None
        self._contract_error: KernelError | None = None
        self._lock = FileLock(config.meta_dir / "kernel.lock")

    # ------------------------------------------------------------- lifecycle
    @classmethod
    def open(cls, ledger_dir: Path | str, *, fault_hook: FaultHook | None = None) -> "Kernel":
        config = Config.load(Path(ledger_dir))
        store = Store(config)
        return cls(config, store, fault_hook=fault_hook)

    @classmethod
    def init(
        cls,
        root_file: Path | str,
        write_target: str,
        *,
        ledger_id: str | None = None,
        approval_carry_forward: bool = True,
        fault_hook: FaultHook | None = None,
    ) -> "Kernel":
        root = Path(root_file).expanduser().resolve()
        if not root.is_file():
            raise KernelError(LEDGER_LOAD_FAILED, f"ledger root file not found: {root}")
        ledger_dir = root.parent
        relative_root = root.name
        target_rel = Path(write_target)
        if target_rel.is_absolute() or ".." in target_rel.parts:
            raise KernelError(WRITE_TARGET_INVALID, "write_target must be a normalized relative path")
        raw_target = ledger_dir / target_rel
        if raw_target.is_symlink():
            raise KernelError(WRITE_TARGET_INVALID, "write_target must not be a symbolic link")
        target = raw_target.resolve()
        if not is_within(ledger_dir, target):
            raise KernelError(WRITE_TARGET_INVALID, "write_target escapes the ledger directory")
        if not target.is_file():
            raise KernelError(WRITE_TARGET_INVALID, f"write_target does not exist: {write_target}")
        if (ledger_dir / ".ledger-kernel" / "kernel.toml").exists():
            raise KernelError(
                "CONFIG_INVALID",
                f"{ledger_dir} is already bound to a LedgerKernel; refusing to overwrite the binding",
            )
        # Probe the target encoding before creating any metadata.
        read_text_file(target)

        adapter = BeancountAdapter(ledger_dir)
        initial_load = adapter.load(root)
        target_key = target.relative_to(ledger_dir).as_posix()
        if target_key not in initial_load.manifest:
            raise KernelError(
                WRITE_TARGET_INVALID,
                f"write_target is not in the root file's include closure: {write_target}",
                {"write_target": write_target, "closure": sorted(initial_load.manifest)},
            )

        config = Config.create(
            ledger_dir,
            relative_root,
            target_key,
            ledger_id=ledger_id,
            approval_carry_forward=approval_carry_forward,
        )
        store = Store(config)
        store.init_layout()
        config.save()
        kernel = cls(config, store, fault_hook=fault_hook)
        kernel._install_initial_state(initial_load)
        store.store_manifest_blobs(config.ledger_dir, initial_load.manifest)
        store.audit_append(
            "LedgerBound",
            {
                "ledger_id": config.ledger_id,
                "root_file": config.root_file,
                "write_target": config.write_target,
                "ledger_revision": initial_load.revision,
                "contract_revision": contract_revision(config.contracts_path),
                "engine_fingerprint": initial_load.engine_fingerprint,
                "ledger_load_ok": initial_load.ok,
                "beancount_errors": len(initial_load.errors),
            },
        )
        kernel._load = initial_load
        return kernel

    def _install_initial_state(self, load: LoadResult) -> None:
        state = {
            "schema_version": 1,
            "ledger_id": self.config.ledger_id,
            "ledger_revision": load.revision,
            "contract_revision": contract_revision(self.config.contracts_path),
            "engine_fingerprint": load.engine_fingerprint,
            "manifest": load.manifest,
            "status": (
                "RecoveryRequired"
                if self._pending_journals_locked()
                else ("Clean" if load.ok else "LedgerInvalid")
            ),
            "last_scan_at": now_iso(),
            "last_load_ok": load.ok,
            "last_errors": load.error_records(load.errors)[:50],
            "last_observation_id": None,
            "last_commit_id": None,
            "kernel_version": __version__,
        }
        self.store.save_state(state)
        self._state = state

    def close(self) -> None:  # pragma: no cover - symmetric no-op
        return None

    @property
    def lock_path(self) -> Path:
        return self.config.meta_dir / "kernel.lock"

    # --------------------------------------------------------------- locking
    def _locked(self) -> FileLock:
        return self._lock

    # ---------------------------------------------------------------- status
    def get_status(self) -> dict[str, Any]:
        with self._locked():
            return self._status_locked()

    def _status_locked(self) -> dict[str, Any]:
        load = self._ensure_fresh_locked()
        state = self._state or {}
        drafts = self.store.list_drafts()
        counts = Counter(self.store.load_draft_state(draft["draft_id"]).get("state") for draft in drafts)
        pending = self._pending_journals_locked()
        audit = self.store.verify_audit()
        return {
            "ledger_id": self.config.ledger_id,
            "ledger_dir": str(self.config.ledger_dir),
            "root_file": self.config.root_file,
            "write_target": self.config.write_target,
            "ledger_revision": load.revision,
            "ledger_revision_short": revision_short(load.revision),
            "contract_revision": contract_revision(self.config.contracts_path),
            "engine_fingerprint": load.engine_fingerprint,
            "status": state.get("status", "Unknown"),
            "ledger_load_ok": load.ok,
            "beancount_errors": load.error_records(load.errors)[:20],
            "beancount_error_count": len(load.errors),
            "contract_error": None if self._contract_error is None else self._contract_error.to_dict(),
            "files_in_closure": len(load.manifest),
            "write_target_in_closure": self.config.write_target in load.manifest,
            "closure_source": load.closure_source,
            "draft_counts": dict(counts),
            "observations": len(self.store.list_observations()),
            "pending_recovery": [journal["commit_id"] for journal in pending],
            "audit_integrity": audit,
            "snapshot_blobs": len(list(self.store.blobs_dir.glob("*"))) if self.store.blobs_dir.exists() else 0,
            "last_scan_at": state.get("last_scan_at"),
            "last_commit_id": state.get("last_commit_id"),
        }

    # ---------------------------------------------------------------- freshness
    def ensure_fresh(self) -> dict[str, Any]:
        with self._locked():
            self._ensure_fresh_locked()
            return dict(self._state or {})

    def _load_contract_locked(self) -> Contract:
        try:
            contract = load_contract(self.config.contracts_path)
        except KernelError as exc:
            self._contract = Contract(accounts={})
            self._contract_error = exc
            raise
        self._contract = contract
        self._contract_error = None
        return contract

    def _read_contract_lenient(self) -> Contract:
        try:
            return self._load_contract_locked()
        except KernelError:
            return Contract(accounts={})

    # --------------------------------------------------------------- contracts
    def contracts_view(self) -> dict[str, Any]:
        """Read-only view: parsed contracts, revision, errors, coverage."""
        with self._locked():
            load = self._ensure_fresh_locked()
            return self._contracts_view_locked(load)

    def _contracts_view_locked(self, load: LoadResult) -> dict[str, Any]:
        contract = self._read_contract_lenient()
        error = None if self._contract_error is None else self._contract_error.to_dict()
        coverage = contract_coverage(contract, load)
        return {
            "contract_path": str(self.config.contracts_path),
            "contract_revision": contract_revision(self.config.contracts_path),
            "schema_version": 1,
            "valid": error is None,
            "error": error,
            "account_count": len(contract.accounts),
            "accounts": {
                name: account_contract_view(policy)
                for name, policy in sorted(contract.accounts.items())
            },
            "unmanaged_accounts": list(contract.unmanaged_accounts),
            "coverage": coverage,
            "warnings": [] if error is not None else contract_warnings(coverage),
        }

    def preview_contracts(self, text: str) -> dict[str, Any]:
        """Strictly parse candidate contracts text and show what apply would do.

        Never writes: used by the contracts validate and diff CLI commands.
        """
        with self._locked():
            load = self._ensure_fresh_locked()
            preview, _contract = self._preview_contracts_locked(load, text)
            return preview

    def _preview_contracts_locked(
        self, load: LoadResult, text: str
    ) -> tuple[dict[str, Any], Contract]:
        path = self.config.contracts_path
        current_revision = contract_revision(path)
        current_text = ""
        if path.exists():
            try:
                current_text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise KernelError(CONTRACT_INVALID, f"cannot read contracts.yaml: {exc}") from exc
        contract = parse_contract_text(text)
        after_revision = contract_revision_bytes(text.encode("utf-8"))
        current_contract = self._read_contract_lenient()
        coverage = contract_coverage(contract, load)
        diff = "\n".join(
            difflib.unified_diff(
                current_text.splitlines(),
                text.splitlines(),
                fromfile=path.name,
                tofile="contracts.new.yaml",
                lineterm="",
            )
        )
        preview = {
            "valid": True,
            "changed": after_revision != current_revision,
            "current_revision": current_revision,
            "contract_revision": after_revision,
            "account_count": len(contract.accounts),
            "accounts": {
                name: account_contract_view(policy)
                for name, policy in sorted(contract.accounts.items())
            },
            "unmanaged_accounts": list(contract.unmanaged_accounts),
            "coverage": coverage,
            "warnings": contract_warnings(coverage),
            "affected": _affected_contracts(current_contract, contract),
            "diff": diff,
        }
        return preview, contract

    def apply_contracts(
        self,
        text: str,
        *,
        expected_current: str | None = None,
        confirm: str | None = None,
        actor: str = "local-user",
        note: str | None = None,
    ) -> dict[str, Any]:
        """Replace contracts.yaml with the given text under dual-hash CAS.

        Both hashes are mandatory: expected_current (the revision the caller
        read, or "absent") and confirm (the revision of the new bytes). The write
        is atomic, preceded by content-addressed blobs, followed by a
        ContractChanged audit event, and it invalidates pending validations.
        """
        with self._locked():
            load = self._ensure_fresh_locked()
            path = self.config.contracts_path
            current_revision = contract_revision(path)

            expected = _canonical_contract_hash(expected_current)
            if expected is None:
                raise KernelError(
                    CONTRACT_CONFLICT,
                    "contracts apply requires --expected-current "
                    "(use 'absent' when no contracts file exists)",
                    {"current_revision": current_revision},
                )
            if expected != current_revision:
                raise KernelError(
                    CONTRACT_CONFLICT,
                    "contracts.yaml changed since it was read; reload and retry",
                    {"expected_current": expected, "current_revision": current_revision},
                )

            preview, contract = self._preview_contracts_locked(load, text)
            after_revision = preview["contract_revision"]
            confirmed = _canonical_contract_hash(confirm)
            if confirmed is None:
                raise KernelError(
                    CONTRACT_CONFLICT,
                    "contracts apply requires --confirm with the SHA-256 of the new contracts file",
                    {"contract_revision": after_revision},
                )
            if confirmed != after_revision:
                raise KernelError(
                    CONTRACT_CONFLICT,
                    "confirmation hash does not match the new contracts file",
                    {"confirm": confirmed, "contract_revision": after_revision},
                )

            audit = self.store.verify_audit()
            if not audit["ok"]:
                raise KernelError(
                    AUDIT_INTEGRITY, f"audit log failed integrity check: {audit['error']}"
                )

            if not preview["changed"]:
                return {
                    **preview,
                    "applied": False,
                    "audit_event_id": None,
                    "previous_revision": current_revision,
                }

            # Recheck the CAS immediately before replacing the file; the lock
            # only coordinates kernels, not human editors.
            if contract_revision(path) != current_revision:
                raise KernelError(
                    CONTRACT_CONFLICT,
                    "contracts.yaml changed during apply; reload and retry",
                    {"expected_current": current_revision},
                )

            new_bytes = text.encode("utf-8")
            if path.exists():
                self.store.put_blob(path.read_bytes())
            self.store.put_blob(new_bytes)
            atomic_write_bytes(path, new_bytes)
            event = self.store.audit_append(
                "ContractChanged",
                {
                    "before_sha256": current_revision,
                    "after_sha256": after_revision,
                    "previous_revision": current_revision,
                    "current_revision": after_revision,
                    "account_count": len(contract.accounts),
                    "unmanaged_account_count": len(contract.unmanaged_accounts),
                    "note": None if note is None else escape_display(str(note))[:500],
                },
                actor={"type": "human", "id": actor},
            )
            # Contract changes invalidate pending validations/approvals; the
            # next operation revalidates under the new contract (ADR-019).
            self._contract = contract
            self._contract_error = None
            self._refresh_drafts_locked(load, after_revision, contract=contract, reason="contract")
            self._state = self._state_from_load(load, after_revision)
            self.store.save_state(self._state)
            return {
                **preview,
                "applied": True,
                "audit_event_id": event["event_id"],
                "previous_revision": current_revision,
            }

    def _ensure_fresh_locked(self) -> LoadResult:
        load = self.adapter.load(self.config.root_path)
        contract_rev = contract_revision(self.config.contracts_path)
        contract = self._read_contract_lenient()
        state = self._state or self.store.load_state()
        if state is None:
            self._install_initial_state(load)
            self._load = load
            self.store.store_manifest_blobs(self.config.ledger_dir, load.manifest)
            return load

        # An unfinished journal means a previous commit may have been applied
        # without its state update. Never reinterpret that as an external edit;
        # block new writes and let recovery decide.
        if self._pending_journals_locked():
            self._state = self._state_from_load(load, contract_rev)
            self.store.save_state(self._state)
            self._load = load
            return load

        manifest_changed = state.get("manifest") != load.manifest
        contract_changed = state.get("contract_revision") != contract_rev
        engine_changed = state.get("engine_fingerprint") != load.engine_fingerprint

        if not manifest_changed and not contract_changed and not engine_changed:
            self._state = state
            self._load = load
            expected = self._expected_status(load)
            changed = False
            if state.get("status") != expected:
                state["status"] = expected
                state["last_scan_at"] = now_iso()
                state["last_errors"] = load.error_records(load.errors)[:50]
                changed = True
            # Even when the ledger is current, drafts validated against an older
            # kernel commit may need a fresh validation (ADR-013 known transition).
            if self._refresh_drafts_locked(load, contract_rev, contract=contract, reason="refresh"):
                changed = True
            if changed:
                self.store.save_state(state)
            return load

        if manifest_changed and not contract_changed and not engine_changed:
            chain = self._transition_chain(state.get("manifest") or {}, load.manifest)
            if chain is not None:
                # Our own committed transition(s) explain the whole delta. No
                # external observation; refresh drafts and approvals instead.
                self._refresh_drafts_locked(load, contract_rev, contract=contract, reason="commit")
                self._state = self._state_from_load(load, contract_rev)
                self.store.save_state(self._state)
                self.store.store_manifest_blobs(self.config.ledger_dir, load.manifest)
                self._load = load
                return load

        if contract_changed:
            self.store.audit_append(
                "ContractChanged",
                {
                    "previous_revision": state.get("contract_revision"),
                    "current_revision": contract_rev,
                },
            )
        if engine_changed:
            self.store.audit_append(
                "EngineChanged",
                {
                    "previous_fingerprint": state.get("engine_fingerprint"),
                    "current_fingerprint": load.engine_fingerprint,
                },
            )

        previous_manifest = state.get("manifest") or {}
        observation = None
        if manifest_changed:
            observation = self._observe_external_change_locked(previous_manifest, load)

        reason = "external" if manifest_changed else "revision"
        if contract_changed:
            reason = "contract"
        if engine_changed:
            reason = "engine"
        self._refresh_drafts_locked(load, contract_rev, contract=contract, reason=reason)

        self._state = self._state_from_load(load, contract_rev)
        if observation:
            self._state["last_observation_id"] = observation["observation_id"]
        self.store.save_state(self._state)
        self.store.store_manifest_blobs(self.config.ledger_dir, load.manifest)
        self._prune_snapshots_locked()
        self._load = load
        return load

    def _expected_status(self, load: LoadResult) -> str:
        if self._contract_error is not None:
            return "ContractInvalid"
        if self._pending_journals_locked():
            return "RecoveryRequired"
        return "LedgerInvalid" if load.errors else "Clean"

    def _state_from_load(self, load: LoadResult, contract_rev: str) -> dict[str, Any]:
        status = "LedgerInvalid" if load.errors else "Clean"
        if self._contract_error is not None:
            status = "ContractInvalid"
        if self._pending_journals_locked():
            status = "RecoveryRequired"
        return {
            "schema_version": 1,
            "ledger_id": self.config.ledger_id,
            "ledger_revision": load.revision,
            "contract_revision": contract_rev,
            "engine_fingerprint": load.engine_fingerprint,
            "manifest": load.manifest,
            "status": status,
            "last_scan_at": now_iso(),
            "last_load_ok": load.ok,
            "last_errors": load.error_records(load.errors)[:50],
            "last_observation_id": (self._state or {}).get("last_observation_id"),
            "last_commit_id": (self._state or {}).get("last_commit_id"),
            "kernel_version": __version__,
        }

    def _prune_snapshots_locked(self) -> None:
        protected: set[str] = set((self._state or {}).get("manifest", {}).values())
        for journal in self.store.list_journals():
            if journal.get("snapshot_ref"):
                protected.add(str(journal["snapshot_ref"]))
            if journal.get("before_hash"):
                protected.add(str(journal["before_hash"]))
            if journal.get("after_hash"):
                protected.add(str(journal["after_hash"]))
        self.store.prune_blobs(protected)

    # -------------------------------------------------------------- transitions
    def _transition_chain(self, start_manifest: Mapping[str, str], current_manifest: Mapping[str, str]) -> list[dict[str, Any]] | None:
        """Return the committed journal chain if it explains the whole delta.

        ADR-013: current revision must equal the previous manifest with only the
        write_target hash replaced by a COMMITTED journal's after_hash.
        """
        if not start_manifest:
            return None
        cursor = dict(start_manifest)
        if cursor == dict(current_manifest):
            return []
        journals = [
            journal
            for journal in self.store.list_journals()
            if journal.get("status") == "COMMITTED"
        ]
        chain: list[dict[str, Any]] = []
        cursor_revision = revision_of(cursor)
        for _ in range(1000):
            if cursor == dict(current_manifest):
                return chain
            matches = [
                journal
                for journal in journals
                if journal.get("before_revision") == cursor_revision
                and journal.get("before_hash") == cursor.get(self.config.write_target)
            ]
            if not matches:
                return None
            journal = matches[-1]
            cursor[self.config.write_target] = str(journal["after_hash"])
            cursor_revision = revision_of(cursor)
            if cursor_revision != journal.get("after_revision"):
                return None
            chain.append(journal)
        return None

    def _validation_is_current(self, validation: Mapping[str, Any], load: LoadResult, contract_rev: str) -> bool:
        return (
            validation.get("ledger_revision") == load.revision
            and validation.get("contract_revision") == contract_rev
            and validation.get("engine_fingerprint") == load.engine_fingerprint
            and validation.get("write_target") == self.config.write_target
        )

    def _refresh_drafts_locked(
        self,
        load: LoadResult,
        contract_rev: str,
        *,
        contract: Contract,
        reason: str,
    ) -> bool:
        """Revalidate or invalidate drafts whose validation is not current.

        Returns True when any workflow state or validation was written.
        """
        changed_any = False
        for draft in self.store.list_drafts():
            draft_id = draft["draft_id"]
            try:
                dstate = self.store.load_draft_state(draft_id)
            except KernelError:
                continue
            if dstate.get("state") in TERMINAL_DRAFT_STATES:
                continue
            if self._draft_expired(draft) and dstate.get("state") not in {"Committed", "Expired"}:
                dstate["state"] = "Expired"
                dstate["updated_at"] = now_iso()
                self.store.save_draft_state(draft_id, dstate)
                self.store.audit_append("DraftExpired", {"draft_id": draft_id})
                changed_any = True
                continue
            validation_id = dstate.get("validation_id")
            if not validation_id:
                continue
            try:
                validation = self.store.load_validation(validation_id)
            except KernelError:
                self._invalidate_draft_locked(
                    draft, dstate, {"validation_id": validation_id}, "validation record is missing", load, contract_rev
                )
                changed_any = True
                continue
            if self._validation_is_current(validation, load, contract_rev):
                if dstate.get("stale"):
                    dstate["stale"] = False
                    dstate["stale_reason"] = None
                    self.store.save_draft_state(draft_id, dstate)
                    changed_any = True
                continue
            if reason in {"contract", "engine"} or self._contract_error is not None:
                self._invalidate_draft_locked(draft, dstate, validation, reason, load, contract_rev)
                changed_any = True
                continue
            chain = self._transition_chain(validation.get("manifest") or {}, load.manifest)
            if chain is None:
                # Any non-kernel change (external edit, sync conflict) invalidates.
                self._invalidate_draft_locked(draft, dstate, validation, "external", load, contract_rev)
                changed_any = True
                continue
            # Known kernel transition: rerun the full dry-run on the new revision.
            try:
                parsed = self._parsed_draft(draft)
                new_validation = self._build_validation_locked(draft, parsed, load, contract)
            except KernelError:
                self._invalidate_draft_locked(draft, dstate, validation, reason, load, contract_rev)
                changed_any = True
                continue
            dstate["validation_id"] = new_validation["validation_id"]
            dstate["stale"] = False
            dstate["stale_reason"] = None
            dstate["updated_at"] = now_iso()
            if new_validation["status"] == "blocked":
                dstate["state"] = "Blocked"
                dstate["approval"] = None
                self.store.audit_append(
                    "DraftInvalidated",
                    {
                        "draft_id": draft_id,
                        "validation_id": new_validation["validation_id"],
                        "reason": "candidate no longer validates after kernel transition",
                    },
                )
            elif dstate.get("state") == "Approved":
                old_digest = validation.get("review_digest")
                new_digest = new_validation.get("review_digest")
                if self.config.approval_carry_forward and old_digest == new_digest:
                    approval = dstate.get("approval") or {}
                    approval["validation_id"] = new_validation["validation_id"]
                    dstate["approval"] = approval
                    self.store.audit_append(
                        "ApprovalCarriedForward",
                        {
                            "approval_ref": approval.get("approval_id"),
                            "from_validation": validation_id,
                            "to_validation": new_validation["validation_id"],
                            "review_digest": new_digest,
                            "via_commit_ids": [str(journal.get("commit_id")) for journal in chain],
                        },
                    )
                else:
                    dstate["state"] = "NeedsHumanApproval"
                    dstate["approval"] = None
                    self.store.audit_append(
                        "DraftInvalidated",
                        {
                            "draft_id": draft_id,
                            "validation_id": validation_id,
                            "reason": "review digest changed across kernel transition"
                            if self.config.approval_carry_forward
                            else "approval carry-forward is disabled",
                        },
                    )
            else:
                dstate["state"] = "NeedsHumanApproval"
            self.store.save_draft_state(draft_id, dstate)
            changed_any = True
        return changed_any

    def _transition_commit_ids_locked(self, start_manifest: Mapping[str, str], current_manifest: Mapping[str, str]) -> list[str]:
        chain = self._transition_chain(start_manifest, current_manifest) or []
        return [str(journal.get("commit_id")) for journal in chain]

    def _invalidate_draft_locked(
        self,
        draft: Mapping[str, Any],
        dstate: dict[str, Any],
        validation: Mapping[str, Any],
        reason: str,
        load: LoadResult,
        contract_rev: str,
    ) -> None:
        draft_id = draft["draft_id"]
        dstate["stale"] = True
        dstate["stale_reason"] = reason
        dstate["updated_at"] = now_iso()
        if dstate.get("state") == "Approved":
            dstate["state"] = "NeedsHumanApproval"
            dstate["approval"] = None
        self.store.save_draft_state(draft_id, dstate)
        self.store.audit_append(
            "DraftInvalidated",
            {
                "draft_id": draft_id,
                "validation_id": validation.get("validation_id"),
                "reason": reason,
                "previous_ledger_revision": validation.get("ledger_revision"),
                "current_ledger_revision": load.revision,
                "previous_contract_revision": validation.get("contract_revision"),
                "current_contract_revision": contract_rev,
            },
        )

    # ------------------------------------------------------------- observation
    def _observe_external_change_locked(self, previous_manifest: Mapping[str, str], load: LoadResult) -> dict[str, Any]:
        changed = diff_manifests(previous_manifest, load.manifest)
        changed_files = sorted(set(changed["added"]) | set(changed["removed"]) | set(changed["changed"]))
        diff_parts: list[str] = []
        unavailable = False
        for relative in changed_files:
            old_hash = previous_manifest.get(relative)
            old_text = self.store.read_blob_text(old_hash) if old_hash else None
            if relative in changed["removed"]:
                new_text = ""
            else:
                path = self.config.ledger_dir / relative
                try:
                    new_text = read_text_file(path).text
                except (OSError, KernelError):
                    new_text = ""
            if old_text is None:
                unavailable = True
                continue
            lines = list(
                difflib.unified_diff(
                    old_text.splitlines(),
                    new_text.splitlines(),
                    fromfile=f"a/{relative}",
                    tofile=f"b/{relative}",
                    lineterm="",
                )
            )
            diff_parts.extend(lines)
        diff_text = "\n".join(diff_parts)
        observation_id = new_id("obs")
        diff_ref = None
        if diff_text:
            diff_ref = f".ledger-kernel/observations/{observation_id}.diff"
        load_status = "valid" if load.ok else "invalid"
        observation = {
            "schema_version": 1,
            "observation_id": observation_id,
            "detected_at": now_iso(),
            "previous_revision": revision_of(previous_manifest),
            "current_revision": load.revision,
            "changed_files": changed_files,
            "diff_ref": diff_ref,
            "diff_unavailable": unavailable or not diff_text,
            "source_kind": "external_unknown",
            "attribution_confidence": "unknown",
            "ledger_load_status": load_status,
            "action_required": not load.ok,
            "event_id": None,
            "beancount_errors": load.error_records(load.errors)[:20],
        }
        event = self.store.audit_append(
            "ExternalChangeObserved",
            {
                "observation_id": observation_id,
                "previous_revision": observation["previous_revision"],
                "current_revision": observation["current_revision"],
                "changed_files": changed_files,
                "diff_ref": diff_ref,
                "diff_unavailable": observation["diff_unavailable"],
                "source_kind": "external_unknown",
                "attribution_confidence": "unknown",
                "ledger_load_status": load_status,
                "action_required": observation["action_required"],
            },
        )
        observation["event_id"] = event["event_id"]
        self.store.save_observation(observation, diff_text or None)
        return observation

    # ------------------------------------------------------------------ drafts
    def submit_draft(
        self,
        payload: Mapping[str, Any],
        idempotency_key: str,
        *,
        agent_id: str = "local-agent",
        agent_version: str = "0.0.0",
    ) -> dict[str, Any]:
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise KernelError(SCHEMA_INVALID, "idempotency_key is required")
        if len(idempotency_key) > 200:
            raise KernelError(SCHEMA_INVALID, "idempotency_key is too long")
        self._validate_agent(agent_id)
        with self._locked():
            self._ensure_fresh_locked()
            parsed = parse_payload(payload, ledger_id=self.config.ledger_id, agent_id=agent_id)
            identity = {"agent_id": agent_id, "agent_version": agent_version}
            key = json.dumps([agent_id, idempotency_key], ensure_ascii=False)
            idempotency = self.store.load_idempotency()
            if key in idempotency:
                existing_id = idempotency[key]
                existing = self.store.load_draft(existing_id)
                return {"draft": existing, "workflow": self.store.load_draft_state(existing_id), "idempotent_replay": True}
            if parsed.supersedes:
                self.store.load_draft(parsed.supersedes)
            draft_id = new_id("drf")
            draft = {
                "schema_version": 1,
                "draft_id": draft_id,
                "created_at": now_iso(),
                "expires_at": None if parsed.expires_at is None else parsed.expires_at.isoformat(),
                "submitted_by": identity,
                "ledger_id": self.config.ledger_id,
                "base_revision": self._load.revision if self._load else None,
                "contract_revision": contract_revision(self.config.contracts_path),
                "operation": "append_transactions",
                "transactions": payload["transactions"],
                "evidence": payload.get("evidence") or [],
                "account_refs": list(parsed.account_refs),
                "confidence": parsed.confidence,
                "rationale_summary": parsed.rationale_summary,
                "supersedes": parsed.supersedes,
                "_kernel": {
                    "payload_sha256": "sha256:" + sha256_bytes(canonical_json_bytes(payload)),
                    "idempotency_key": idempotency_key,
                    "agent_version": agent_version,
                    "received_at": now_iso(),
                },
            }
            self.store.save_draft(draft)
            self.store.save_draft_state(
                draft_id,
                {
                    "draft_id": draft_id,
                    "state": "Submitted",
                    "validation_id": None,
                    "stale": False,
                    "stale_reason": None,
                    "approval": None,
                    "updated_at": now_iso(),
                    "history": [],
                },
            )
            idempotency[key] = draft_id
            self.store.save_idempotency(idempotency)
            self.store.audit_append(
                "DraftSubmitted",
                {
                    "draft_id": draft_id,
                    "agent": identity,
                    "idempotency_key": idempotency_key,
                    "base_revision": draft["base_revision"],
                    "transaction_count": len(parsed.transactions),
                    "accounts": list(parsed.account_refs),
                },
                actor={"type": "agent", "id": agent_id},
            )
            return {"draft": draft, "workflow": self.store.load_draft_state(draft_id), "idempotent_replay": False}

    def _validate_agent(self, agent_id: str) -> None:
        if not isinstance(agent_id, str) or not agent_id.strip() or len(agent_id) > 80:
            raise KernelError(SCHEMA_INVALID, "agent_id must be a non-empty string of at most 80 characters")
        for char in agent_id:
            if ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F:
                raise KernelError(SCHEMA_INVALID, "agent_id contains control characters")

    def get_draft(self, draft_id: str) -> dict[str, Any]:
        with self._locked():
            self._ensure_fresh_locked()
            draft = self.store.load_draft(draft_id)
            state = self.store.load_draft_state(draft_id)
            validation = None
            if state.get("validation_id"):
                try:
                    validation = self.store.load_validation(state["validation_id"])
                except KernelError:
                    validation = None
            return {"draft": draft, "workflow": state, "validation": validation}

    def list_drafts(self, *, state: str | None = None) -> list[dict[str, Any]]:
        with self._locked():
            self._ensure_fresh_locked()
            rows: list[dict[str, Any]] = []
            for draft in self.store.list_drafts():
                workflow = self.store.load_draft_state(draft["draft_id"])
                if state is not None and workflow.get("state") != state:
                    continue
                rows.append(
                    {
                        "draft_id": draft["draft_id"],
                        "created_at": draft.get("created_at"),
                        "state": workflow.get("state"),
                        "stale": workflow.get("stale"),
                        "validation_id": workflow.get("validation_id"),
                        "agent": draft.get("submitted_by", {}).get("agent_id"),
                        "transactions": len(draft.get("transactions") or []),
                        "accounts": draft.get("account_refs") or [],
                        "narration": truncate(
                            escape_display(
                                " / ".join(
                                    str(txn.get("narration", "")) for txn in draft.get("transactions") or []
                                )
                            ),
                            80,
                        ),
                    }
                )
            rows.sort(key=lambda row: row.get("created_at") or "", reverse=True)
            return rows

    def _parsed_draft(self, draft: Mapping[str, Any]) -> ParsedDraft:
        payload = {key: value for key, value in draft.items() if not key.startswith("_")}
        agent_id = (draft.get("submitted_by") or {}).get("agent_id", "local-agent")
        return parse_payload(
            payload,
            ledger_id=self.config.ledger_id,
            agent_id=agent_id,
            allow_past_expiry=True,
        )

    def validate_draft(self, draft_id: str) -> dict[str, Any]:
        with self._locked():
            load = self._ensure_fresh_locked()
            contract = self._require_contract()
            draft = self.store.load_draft(draft_id)
            workflow = self.store.load_draft_state(draft_id)
            if workflow.get("state") in {"Committed", "Rejected", "Withdrawn", "Committing", "RecoveryRequired"}:
                raise KernelError(INVALID_STATE, f"draft is not editable (state={workflow.get('state')})")
            if self._draft_expired(draft):
                workflow["state"] = "Expired"
                workflow["updated_at"] = now_iso()
                self.store.save_draft_state(draft_id, workflow)
                self.store.audit_append("DraftExpired", {"draft_id": draft_id})
                raise KernelError(INVALID_STATE, "draft has expired")
            parsed = self._parsed_draft(draft)
            validation = self._build_validation_locked(draft, parsed, load, contract)
            workflow["validation_id"] = validation["validation_id"]
            workflow["state"] = "Blocked" if validation["status"] == "blocked" else "NeedsHumanApproval"
            workflow["stale"] = False
            workflow["stale_reason"] = None
            workflow["approved"] = None
            workflow["approval"] = None
            workflow["updated_at"] = now_iso()
            self.store.save_draft_state(draft_id, workflow)
            return validation

    def _assert_target_in_closure(self, load: LoadResult) -> None:
        if self.config.write_target not in load.manifest:
            raise KernelError(
                WRITE_TARGET_INVALID,
                "write_target is not in the current include closure; fix kernel.toml/includes before writing",
                {"write_target": self.config.write_target},
            )

    def _build_validation_locked(
        self,
        draft: Mapping[str, Any],
        parsed: ParsedDraft,
        load: LoadResult,
        contract: Contract,
    ) -> dict[str, Any]:
        self._assert_target_in_closure(load)
        contract_rev = contract_revision(self.config.contracts_path)
        result = dry_run(
            ledger_dir=self.config.ledger_dir,
            write_target=self.config.write_target,
            baseline=load,
            adapter_factory=BeancountAdapter,
            parsed=parsed,
            agent_id=(draft.get("submitted_by") or {}).get("agent_id", "local-agent"),
            contract=contract,
            fault_hook=self.fault_hook,
        )
        validation = {
            "schema_version": 1,
            "validation_id": new_id("val"),
            "draft_id": draft["draft_id"],
            "created_at": now_iso(),
            "ledger_revision": load.revision,
            "contract_revision": contract_rev,
            "engine_fingerprint": load.engine_fingerprint,
            "write_target": self.config.write_target,
            "status": result.status,
            "checks": result.checks,
            "diff": result.diff,
            "fragment": result.fragment,
            "fragment_sha256": result.fragment_sha256,
            "review_digest": result.review_digest,
            "manifest": load.manifest,
            "agent_id": (draft.get("submitted_by") or {}).get("agent_id"),
        }
        self.store.save_validation(validation)
        self.store.audit_append(
            "DraftValidated",
            {
                "draft_id": draft["draft_id"],
                "validation_id": validation["validation_id"],
                "status": validation["status"],
                "ledger_revision": validation["ledger_revision"],
                "contract_revision": validation["contract_revision"],
                "fragment_sha256": validation["fragment_sha256"],
                "review_digest": validation["review_digest"],
                "blocking_errors": [c["code"] for c in result.checks if c.get("severity") == "block"],
            },
        )
        return validation

    def get_draft_diff(self, draft_id: str, validation_id: str | None = None) -> dict[str, Any]:
        entry = self.get_draft(draft_id)
        if validation_id is None:
            validation = entry.get("validation")
        else:
            validation = self.store.load_validation(validation_id)
        if validation is None or validation.get("draft_id") != draft_id:
            raise KernelError("VALIDATION_NOT_FOUND", "no validation for this draft")
        return {
            "validation_id": validation["validation_id"],
            "draft_id": draft_id,
            "status": validation["status"],
            "ledger_revision": validation["ledger_revision"],
            "contract_revision": validation["contract_revision"],
            "checks": validation.get("checks") or [],
            "diff": validation.get("diff") or {},
            "fragment_sha256": validation.get("fragment_sha256"),
        }

    def _draft_expired(self, draft: Mapping[str, Any]) -> bool:
        expires = draft.get("expires_at")
        if not expires:
            return False
        try:
            return parse_datetime(str(expires)) <= _utcnow()
        except ValueError:
            return True

    # ----------------------------------------------------------- approval
    def approve_draft(
        self,
        draft_id: str,
        *,
        validation_id: str | None = None,
        expected_ledger_revision: str | None = None,
        expected_contract_revision: str | None = None,
        actor: str = "local-user",
        note: str | None = None,
    ) -> dict[str, Any]:
        with self._locked():
            load = self._ensure_fresh_locked()
            return self._approve_locked(
                draft_id,
                load,
                validation_id=validation_id,
                expected_ledger_revision=expected_ledger_revision,
                expected_contract_revision=expected_contract_revision,
                actor=actor,
                note=note,
            )

    def approve_drafts(self, draft_ids: Iterable[str], *, actor: str = "local-user", note: str | None = None) -> list[dict[str, Any]]:
        with self._locked():
            load = self._ensure_fresh_locked()
            return [
                self._approve_locked(draft_id, load, actor=actor, note=note, validation_id=None,
                                     expected_ledger_revision=None, expected_contract_revision=None)
                for draft_id in draft_ids
            ]

    def _approve_locked(
        self,
        draft_id: str,
        load: LoadResult,
        *,
        validation_id: str | None,
        expected_ledger_revision: str | None,
        expected_contract_revision: str | None,
        actor: str,
        note: str | None,
    ) -> dict[str, Any]:
        self._require_contract()
        draft = self.store.load_draft(draft_id)
        workflow = self.store.load_draft_state(draft_id)
        if workflow.get("state") != "NeedsHumanApproval":
            raise KernelError(
                INVALID_STATE,
                f"draft is not awaiting approval (state={workflow.get('state')})",
                {"draft_id": draft_id, "state": workflow.get("state")},
            )
        if self._draft_expired(draft):
            workflow["state"] = "Expired"
            workflow["updated_at"] = now_iso()
            self.store.save_draft_state(draft_id, workflow)
            self.store.audit_append("DraftExpired", {"draft_id": draft_id})
            raise KernelError(INVALID_STATE, "draft has expired")
        selected = validation_id or workflow.get("validation_id")
        if not selected:
            raise KernelError("VALIDATION_NOT_FOUND", "draft has no validation to approve")
        validation = self.store.load_validation(selected)
        if validation.get("draft_id") != draft_id:
            raise KernelError("VALIDATION_NOT_FOUND", "validation does not belong to this draft")
        if validation.get("status") == "blocked":
            raise KernelError(INVALID_STATE, "blocked validation cannot be approved")
        if validation.get("write_target") != self.config.write_target:
            raise KernelError(CONFIG_INVALID, "validation was produced for a different write_target")
        contract_rev = contract_revision(self.config.contracts_path)
        if not self._validation_is_current(validation, load, contract_rev):
            raise KernelError(
                STALE_REVISION,
                "validation is stale; rerun validate before approving",
                {
                    "validation_ledger_revision": validation.get("ledger_revision"),
                    "current_ledger_revision": load.revision,
                    "validation_contract_revision": validation.get("contract_revision"),
                    "current_contract_revision": contract_rev,
                },
            )
        if expected_ledger_revision and expected_ledger_revision != load.revision:
            raise KernelError(STALE_REVISION, "ledger revision changed since the reviewer looked at it")
        if expected_contract_revision and expected_contract_revision != contract_rev:
            raise KernelError(STALE_REVISION, "contract revision changed since the reviewer looked at it")
        approval = {
            "approval_id": new_id("apr"),
            "validation_id": validation["validation_id"],
            "actor": {"type": "human", "id": actor},
            "approved_at": now_iso(),
            "expires_at": (_utcnow() + dt.timedelta(seconds=self.config.approval_ttl_seconds)).isoformat(),
            "note": note,
        }
        workflow["state"] = "Approved"
        workflow["approval"] = approval
        workflow["stale"] = False
        workflow["stale_reason"] = None
        workflow["updated_at"] = now_iso()
        self.store.save_draft_state(draft_id, workflow)
        self.store.audit_append(
            "DraftApproved",
            {
                "draft_id": draft_id,
                "validation_id": validation["validation_id"],
                "approval_ref": approval["approval_id"],
                "ledger_revision": load.revision,
                "contract_revision": contract_rev,
                "review_digest": validation.get("review_digest"),
            },
            actor={"type": "human", "id": actor},
        )
        return {"draft_id": draft_id, "approval": approval, "workflow": workflow}

    def reject_draft(self, draft_id: str, reason: str, *, actor: str = "local-user") -> dict[str, Any]:
        if not isinstance(reason, str) or not reason.strip():
            raise KernelError(SCHEMA_INVALID, "a rejection reason is required")
        with self._locked():
            self._ensure_fresh_locked()
            workflow = self.store.load_draft_state(draft_id)
            if workflow.get("state") in TERMINAL_DRAFT_STATES:
                raise KernelError(INVALID_STATE, f"draft is already {workflow.get('state')}")
            workflow["state"] = "Rejected"
            workflow["approval"] = None
            workflow["updated_at"] = now_iso()
            self.store.save_draft_state(draft_id, workflow)
            self.store.audit_append(
                "DraftRejected",
                {"draft_id": draft_id, "reason": escape_display(reason)[:500]},
                actor={"type": "human", "id": actor},
            )
            return workflow

    def withdraw_draft(self, draft_id: str, *, agent_id: str | None = None) -> dict[str, Any]:
        with self._locked():
            self._ensure_fresh_locked()
            draft = self.store.load_draft(draft_id)
            workflow = self.store.load_draft_state(draft_id)
            if workflow.get("state") in TERMINAL_DRAFT_STATES:
                raise KernelError(INVALID_STATE, f"draft is already {workflow.get('state')}")
            if workflow.get("state") == "Approved":
                raise KernelError(INVALID_STATE, "approved drafts cannot be withdrawn; reject or commit instead")
            if agent_id is not None and draft.get("submitted_by", {}).get("agent_id") != agent_id:
                raise KernelError(INVALID_STATE, "only the submitting agent may withdraw this draft")
            workflow["state"] = "Withdrawn"
            workflow["approval"] = None
            workflow["updated_at"] = now_iso()
            self.store.save_draft_state(draft_id, workflow)
            self.store.audit_append(
                "DraftWithdrawn",
                {"draft_id": draft_id, "agent_id": agent_id},
                actor={"type": "agent", "id": agent_id} if agent_id else None,
            )
            return workflow

    # -------------------------------------------------------------- accounts
    def list_accounts(self, as_of: dt.date | None = None) -> list[dict[str, Any]]:
        with self._locked():
            load = self._ensure_fresh_locked()
            contract = self._read_contract_lenient()
            balances = self.adapter.balances(load, as_of or dt.date.today())
            pending = self._pending_counts_locked()
            rows: list[dict[str, Any]] = []
            for name, info in sorted(load.accounts.items()):
                policy = contract.policy(name)
                rows.append(
                    {
                        "account": name,
                        "lifecycle": _lifecycle(info),
                        "open_date": info.open_date.isoformat() if info.open_date else None,
                        "close_date": info.close_date.isoformat() if info.close_date else None,
                        "currencies": list(info.currencies),
                        "booking": info.booking,
                        "balances": {
                            currency: format(amount, "f")
                            for (account, currency), amount in sorted(balances.items())
                            if account == name
                        },
                        "aliases": list(policy.aliases) if policy else [],
                        "contract": account_contract_view(policy),
                        "pending_drafts": pending.get(name, 0),
                    }
                )
            return rows

    def get_account(self, account_name: str, as_of: dt.date | None = None) -> dict[str, Any]:
        with self._locked():
            load = self._ensure_fresh_locked()
            contract = self._read_contract_lenient()
            info = load.accounts.get(account_name)
            policy = contract.policy(account_name)
            balances = self.adapter.account_balances(load, account_name, as_of or dt.date.today())
            last_dates = self.adapter.last_posting_dates(load)
            pending = self._pending_counts_locked().get(account_name, 0)
            if info is None and policy is None:
                raise KernelError("ACCOUNT_NOT_FOUND", f"unknown account: {account_name}")
            return {
                "account": account_name,
                "lifecycle": _lifecycle(info) if info else "not_open",
                "definition": None
                if info is None
                else {
                    "open_date": info.open_date.isoformat() if info.open_date else None,
                    "close_date": info.close_date.isoformat() if info.close_date else None,
                    "currencies": list(info.currencies),
                    "booking": info.booking,
                    "source_file": info.source_file,
                    "source_line": info.source_line,
                },
                "balances": {currency: format(amount, "f") for currency, amount in sorted(balances.items())},
                "last_posting_date": last_dates[account_name].isoformat() if account_name in last_dates else None,
                "aliases": list(policy.aliases) if policy else [],
                "contract": account_contract_view(policy),
                "pending_drafts": pending,
                "note": "balances are derived from the ledger; contracts never store balances",
            }

    def get_balance(self, account_name: str, as_of: dt.date | None = None) -> dict[str, Any]:
        with self._locked():
            load = self._ensure_fresh_locked()
            balances = self.adapter.account_balances(load, account_name, as_of)
            return {
                "account": account_name,
                "as_of": (as_of or dt.date.today()).isoformat(),
                "balances": {currency: format(amount, "f") for currency, amount in sorted(balances.items())},
                "ledger_revision": load.revision,
            }

    def _pending_counts_locked(self) -> dict[str, int]:
        counts: Counter = Counter()
        for draft in self.store.list_drafts():
            workflow = self.store.load_draft_state(draft["draft_id"])
            if workflow.get("state") in {"Submitted", "NeedsHumanApproval", "Approved"}:
                for account in draft.get("account_refs") or []:
                    counts[account] += 1
        return dict(counts)

    # -------------------------------------------------------------- scanning
    def scan_ledger(self) -> dict[str, Any]:
        with self._locked():
            load = self._ensure_fresh_locked()
            return {
                "ledger_revision": load.revision,
                "status": (self._state or {}).get("status"),
                "beancount_errors": load.error_records(load.errors)[:20],
                "observations": len(self.store.list_observations()),
                "last_observation_id": (self._state or {}).get("last_observation_id"),
            }

    def list_observations(self) -> list[dict[str, Any]]:
        with self._locked():
            self._ensure_fresh_locked()
            return self.store.list_observations()

    def get_observation(self, observation_id: str) -> dict[str, Any]:
        with self._locked():
            self._ensure_fresh_locked()
            observation = self.store.load_observation(observation_id)
            diff = None
            if observation.get("diff_ref"):
                path = (self.config.ledger_dir / str(observation["diff_ref"])).resolve()
                if is_within(self.config.ledger_dir, path) and path.exists():
                    diff = path.read_text(encoding="utf-8")
            return {"observation": observation, "diff": diff}

    # --------------------------------------------------------------- commit
    def _require_contract(self) -> Contract:
        try:
            return self._load_contract_locked()
        except KernelError as exc:
            raise KernelError(CONTRACT_INVALID, exc.message, exc.details) from exc

    def _pending_journals_locked(self) -> list[dict[str, Any]]:
        return [
            journal
            for journal in self.store.list_journals()
            if journal.get("status") in {"PREPARED", "RECOVERY_REQUIRED"}
        ]

    def _assert_ready_to_commit_locked(self) -> None:
        audit = self.store.verify_audit()
        if not audit["ok"]:
            raise KernelError(AUDIT_INTEGRITY, f"audit log failed integrity check: {audit['error']}")
        if self._contract_error is not None:
            raise KernelError(CONTRACT_INVALID, self._contract_error.message)
        pending = self._pending_journals_locked()
        if pending:
            raise KernelError(
                RECOVERY_REQUIRED,
                "unfinished commit journal(s) must be recovered before new commits",
                {"commit_ids": [journal["commit_id"] for journal in pending]},
            )
        state = self._state or {}
        if state.get("status") == "LedgerInvalid":
            raise KernelError(LEDGER_INVALID, "ledger is invalid; controlled commits are blocked")
        if state.get("status") == "RecoveryRequired":
            raise KernelError(RECOVERY_REQUIRED, "kernel is in recovery state")
        if state.get("status") == "ContractInvalid":
            raise KernelError(CONTRACT_INVALID, "contracts are invalid; controlled commits are blocked")

    def commit_draft(self, draft_id: str) -> dict[str, Any]:
        with self._locked():
            load = self._ensure_fresh_locked()
            self._assert_ready_to_commit_locked()
            return self._commit_locked(draft_id, load)

    def commit_drafts(self, draft_ids: Iterable[str]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for draft_id in draft_ids:
            results.append(self.commit_draft(draft_id))
        return results

    def _commit_locked(self, draft_id: str, load: LoadResult) -> dict[str, Any]:
        draft = self.store.load_draft(draft_id)
        workflow = self.store.load_draft_state(draft_id)
        if workflow.get("state") != "Approved":
            raise KernelError(
                APPROVAL_REQUIRED,
                f"draft is not approved (state={workflow.get('state')})",
                {"draft_id": draft_id},
            )
        approval = workflow.get("approval") or {}
        if not approval.get("approval_id"):
            raise KernelError(APPROVAL_REQUIRED, "approval record is missing")
        if approval.get("expires_at") and parse_datetime(approval["expires_at"]) <= _utcnow():
            raise KernelError(APPROVAL_EXPIRED, "approval has expired; review and approve again")
        validation = self.store.load_validation(approval.get("validation_id") or workflow.get("validation_id"))
        if validation.get("draft_id") != draft_id:
            raise KernelError("VALIDATION_NOT_FOUND", "approval points to a different draft")
        if validation.get("status") == "blocked":
            raise KernelError(INVALID_STATE, "cannot commit a blocked validation")
        if validation.get("write_target") != self.config.write_target:
            raise KernelError(CONFIG_INVALID, "validation was produced for a different write_target")
        if not self._validation_is_current(validation, load, contract_revision(self.config.contracts_path)):
            raise KernelError(
                STALE_REVISION,
                "ledger or contract changed after approval; rerun validation and approval",
            )
        if self._draft_expired(draft):
            raise KernelError(INVALID_STATE, "draft has expired")
        # The stored fragment itself must still hash to the validated value.
        verify_fragment(str(validation.get("fragment", "")), validation["fragment_sha256"])

        self._assert_target_in_closure(load)
        parsed = self._parsed_draft(draft)
        target_path = self.config.write_target_path
        if not target_path.is_file():
            raise KernelError(WRITE_TARGET_INVALID, "write_target disappeared")
        target_info = read_text_file(target_path)
        rendered = render_fragment(parsed.transactions, target_info.newline)
        fragment = fragment_for(target_info.text, rendered, target_info.newline)
        verify_fragment(fragment, validation["fragment_sha256"])
        new_text = target_info.text + fragment
        new_bytes = encode_text_file(new_text, target_info)
        after_hash = sha256_bytes(new_bytes)
        before_hash = sha256_file(target_path)
        if load.manifest.get(self.config.write_target) != before_hash:
            raise KernelError(EXTERNAL_CHANGE, "write_target changed before commit; rescan required")

        new_manifest = dict(load.manifest)
        new_manifest[self.config.write_target] = after_hash
        after_revision = revision_of(new_manifest, load.engine_fingerprint)

        # Commit-time second full validation in a fresh workspace.
        temp_root = Path(tempfile.mkdtemp(prefix="ledger-kernel-commit-"))
        try:
            copy_ledger_workspace(load, temp_root)
            (temp_root / self.config.write_target).write_bytes(new_bytes)
            candidate = BeancountAdapter(temp_root).load(temp_root / self.config.root_file)
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)
        ok, problems = verify_candidate(
            candidate=candidate,
            baseline=load,
            parsed=parsed,
            write_target=self.config.write_target,
        )
        if not ok:
            self.store.audit_append(
                "CommitFailed",
                {"draft_id": draft_id, "validation_id": validation["validation_id"], "problems": problems},
            )
            raise KernelError(DELTA_MISMATCH, "commit-time validation rejected the candidate", {"problems": problems})

        commit_id = new_id("cmt")
        snapshot = self.store.put_blob(target_path.read_bytes())
        ensure_dir(self.store.snapshot_dir(commit_id))
        snapshot_copy = self.store.snapshot_dir(commit_id) / "before.bin"
        snapshot_copy.write_bytes(self.store.read_blob(snapshot))

        journal = {
            "schema_version": 1,
            "commit_id": commit_id,
            "draft_id": draft_id,
            "validation_id": validation["validation_id"],
            "approval_ref": approval.get("approval_id"),
            "status": "PREPARED",
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "ledger_id": self.config.ledger_id,
            "write_target": self.config.write_target,
            "before_revision": load.revision,
            "after_revision": after_revision,
            "before_hash": before_hash,
            "after_hash": after_hash,
            "expected_after_hash": after_hash,
            "fragment_sha256": "sha256:" + sha256_bytes(fragment.encode("utf-8")),
            "snapshot_ref": snapshot,
            "snapshot_path": str(snapshot_copy.relative_to(self.config.ledger_dir)),
            "temp_path": None,
        }
        self.store.save_journal(journal)
        self.store.audit_append(
            "CommitPrepared",
            {
                "draft_id": draft_id,
                "commit_id": commit_id,
                "validation_id": validation["validation_id"],
                "approval_ref": approval.get("approval_id"),
                "before_revision": load.revision,
                "after_revision": after_revision,
                "before_hash": before_hash,
                "after_hash": after_hash,
                "write_target": self.config.write_target,
                "fragment_sha256": journal["fragment_sha256"],
            },
            actor={"type": "kernel", "id": "ledger-kernel"},
        )

        workflow["state"] = "Committing"
        workflow["updated_at"] = now_iso()
        self.store.save_draft_state(draft_id, workflow)

        temp_file: Path | None = None
        replaced = False
        try:
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{target_path.name}.", suffix=".lk-tmp", dir=target_path.parent
            )
            temp_file = Path(temp_name)
            with os.fdopen(fd, "wb") as handle:
                handle.write(new_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_file, target_path.stat().st_mode & 0o777)
            journal["temp_path"] = str(temp_file)
            journal["updated_at"] = now_iso()
            self.store.save_journal(journal)

            if sha256_file(target_path) != before_hash:
                raise KernelError(EXTERNAL_CHANGE, "write_target changed during commit preparation")
            # Final full-ledger check: a change to any include file between the
            # approval and this point invalidates the validation, even if the
            # change is comment-only and does not alter entries.
            recheck = BeancountAdapter(self.config.ledger_dir).load(self.config.root_path)
            if recheck.revision != load.revision:
                raise KernelError(
                    EXTERNAL_CHANGE,
                    "ledger revision changed before replace; commit aborted",
                    {"expected": load.revision, "actual": recheck.revision},
                )
            if contract_revision(self.config.contracts_path) != validation.get("contract_revision"):
                raise KernelError(EXTERNAL_CHANGE, "contracts changed before replace; commit aborted")
            if self.fault_hook is not None:
                self.fault_hook("before_replace", {"commit_id": commit_id})
            os.replace(temp_file, target_path)
            replaced = True
            temp_file = None
            fsync_dir(target_path.parent)
            if self.fault_hook is not None:
                self.fault_hook("after_replace", {"commit_id": commit_id})
            if sha256_file(target_path) != after_hash:
                raise KernelError(RECOVERY_REQUIRED, "post-replace hash mismatch")
            if self.fault_hook is not None:
                self.fault_hook("after_verify", {"commit_id": commit_id})

            post_load = BeancountAdapter(self.config.ledger_dir).load(self.config.root_path)
            if post_load.revision != after_revision:
                raise KernelError(
                    RECOVERY_REQUIRED,
                    "ledger revision after replace does not match the prepared journal",
                    {"expected": after_revision, "actual": post_load.revision},
                )
            if not post_load.ok:
                raise KernelError(
                    RECOVERY_REQUIRED,
                    "ledger does not load cleanly after replace",
                    {"errors": post_load.error_records()[:20]},
                )
            self._load = post_load

            journal["status"] = "COMMITTED"
            journal["committed_at"] = now_iso()
            journal["updated_at"] = now_iso()
            self.store.save_journal(journal)

            self._state = self._state_from_load(self._load, contract_revision(self.config.contracts_path))
            self._state["last_commit_id"] = commit_id
            self.store.save_state(self._state)
            self.store.store_manifest_blobs(self.config.ledger_dir, self._load.manifest)
            self._prune_snapshots_locked()

            workflow["state"] = "Committed"
            workflow["commit_id"] = commit_id
            workflow["committed_at"] = now_iso()
            workflow["approval"] = None
            workflow["updated_at"] = now_iso()
            self.store.save_draft_state(draft_id, workflow)
            event = self.store.audit_append(
                "DraftCommitted",
                {
                    "draft_id": draft_id,
                    "commit_id": commit_id,
                    "validation_id": validation["validation_id"],
                    "agent": draft.get("submitted_by"),
                    "ledger_revision_before": journal["before_revision"],
                    "ledger_revision_after": after_revision,
                    "contract_revision": contract_revision(self.config.contracts_path),
                    "changed_files": [self.config.write_target],
                    "before_hash": before_hash,
                    "after_hash": after_hash,
                    "approval_ref": approval.get("approval_id"),
                    "fragment_sha256": journal["fragment_sha256"],
                },
                actor={"type": "human", "id": approval.get("actor", {}).get("id", "local-user")},
            )
            if self.fault_hook is not None:
                self.fault_hook("after_commit", {"commit_id": commit_id})
            return {
                "commit_id": commit_id,
                "draft_id": draft_id,
                "validation_id": validation["validation_id"],
                "ledger_revision_before": journal["before_revision"],
                "ledger_revision_after": after_revision,
                "before_hash": before_hash,
                "after_hash": after_hash,
                "write_target": self.config.write_target,
                "audit_event_id": event["event_id"],
            }
        except BaseException as exc:
            if replaced:
                journal["status"] = "RECOVERY_REQUIRED"
                journal["updated_at"] = now_iso()
                journal["error"] = str(exc)
                self.store.save_journal(journal)
                self.store.audit_append(
                    "RecoveryRequired",
                    {
                        "commit_id": commit_id,
                        "draft_id": draft_id,
                        "reason": str(exc),
                        "target_hash": sha256_file(target_path) if target_path.exists() else None,
                        "expected_after_hash": after_hash,
                        "before_hash": before_hash,
                    },
                    actor={"type": "kernel", "id": "ledger-kernel"},
                )
                workflow["state"] = "RecoveryRequired"
                workflow["updated_at"] = now_iso()
                self.store.save_draft_state(draft_id, workflow)
                if self._state is not None:
                    self._state["status"] = "RecoveryRequired"
                    self.store.save_state(self._state)
            else:
                workflow["state"] = "Approved"
                workflow["updated_at"] = now_iso()
                self.store.save_draft_state(draft_id, workflow)
                journal["status"] = "ABORTED"
                journal["updated_at"] = now_iso()
                journal["error"] = str(exc)
                self.store.save_journal(journal)
                self.store.audit_append(
                    "CommitFailed",
                    {"draft_id": draft_id, "commit_id": commit_id, "reason": str(exc)},
                )
                if temp_file is not None and temp_file.exists():
                    try:
                        temp_file.unlink()
                    except OSError:
                        pass
            raise

    # ------------------------------------------------------------ corrections
    def propose_reversal(self, commit_id: str, *, actor: str = "local-user") -> dict[str, Any]:
        """Create a *draft* that reverses a kernel commit.

        This never edits or deletes the original transaction. The reversal is a
        normal draft that must pass validation, human approval and the normal
        controlled commit path.
        """
        with self._locked():
            self._ensure_fresh_locked()
            journal = self.store.load_journal(commit_id)
            if journal.get("status") != "COMMITTED":
                raise KernelError(INVALID_STATE, "only committed kernel writes can be reversed")
            original = self.store.load_draft(journal["draft_id"])
        parsed = self._parsed_draft(original)
        today = max(dt.date.today(), max((txn.date for txn in parsed.transactions), default=dt.date.today()))
        transactions = []
        for transaction in parsed.transactions:
            postings = [
                {
                    "account": posting.account,
                    "units": format(-posting.units, "f"),
                    "currency": posting.currency,
                }
                for posting in transaction.postings
            ]
            transactions.append(
                {
                    "date": today.isoformat(),
                    "flag": "*",
                    "payee": transaction.payee,
                    "narration": f"Reversal: {transaction.narration}"[:600],
                    "links": [commit_id],
                    "postings": postings,
                }
            )
        payload = {
            "schema_version": 1,
            "operation": "append_transactions",
            "transactions": transactions,
            "rationale_summary": f"Reversal proposal for kernel commit {commit_id}; requires review.",
            "account_refs": sorted({posting["account"] for txn in transactions for posting in txn["postings"]}),
        }
        result = self.submit_draft(
            payload,
            f"reversal:{commit_id}",
            agent_id="ledger-kernel-reversal",
        )
        if not result.get("idempotent_replay"):
            with self._locked():
                self.store.audit_append(
                    "CorrectionProposed",
                    {
                        "commit_id": commit_id,
                        "original_draft_id": original["draft_id"],
                        "reversal_draft_id": result["draft"]["draft_id"],
                    },
                    actor={"type": "human", "id": actor},
                )
        return result

    # -------------------------------------------------------------- recovery
    def recovery_status(self) -> dict[str, Any]:
        with self._locked():
            load = self._ensure_fresh_locked()
            rows = []
            for journal in self.store.list_journals():
                if journal.get("status") in {"PREPARED", "RECOVERY_REQUIRED"}:
                    target = self.config.ledger_dir / journal["write_target"]
                    current_hash = sha256_file(target) if target.exists() else None
                    outcome = "unknown"
                    if current_hash == journal.get("after_hash"):
                        outcome = "applied"
                    elif current_hash == journal.get("before_hash"):
                        outcome = "not_applied"
                    rows.append(
                        {
                            "commit_id": journal["commit_id"],
                            "draft_id": journal["draft_id"],
                            "status": journal["status"],
                            "write_target": journal["write_target"],
                            "current_hash": current_hash,
                            "before_hash": journal.get("before_hash"),
                            "after_hash": journal.get("after_hash"),
                            "outcome": outcome,
                            "created_at": journal.get("created_at"),
                        }
                    )
            return {
                "status": "RecoveryRequired" if rows else "Clean",
                "ledger_revision": load.revision,
                "pending": rows,
            }

    def recover(self, commit_id: str | None = None) -> list[dict[str, Any]]:
        with self._locked():
            self._ensure_fresh_locked()
            journals = [
                journal
                for journal in self.store.list_journals()
                if journal.get("status") in {"PREPARED", "RECOVERY_REQUIRED"}
                and (commit_id is None or journal.get("commit_id") == commit_id)
            ]
            results: list[dict[str, Any]] = []
            for journal in journals:
                results.append(self._recover_one_locked(journal))
            self._load = self.adapter.load(self.config.root_path)
            self._state = self._state_from_load(self._load, contract_revision(self.config.contracts_path))
            self.store.save_state(self._state)
            self.store.store_manifest_blobs(self.config.ledger_dir, self._load.manifest)
            return results

    def _recover_one_locked(self, journal: dict[str, Any]) -> dict[str, Any]:
        commit_id = journal["commit_id"]
        raw_target = self.config.ledger_dir / str(journal.get("write_target", ""))
        target = raw_target.resolve()
        if not is_within(self.config.ledger_dir, target):
            journal["status"] = "RECOVERY_REQUIRED"
            journal["updated_at"] = now_iso()
            journal["error"] = "journal write_target escapes the ledger directory"
            self.store.save_journal(journal)
            self.store.audit_append(
                "RecoveryRequired",
                {"commit_id": commit_id, "reason": journal["error"]},
            )
            return {"commit_id": commit_id, "outcome": "recovery_required", "reason": journal["error"]}
        current_hash = sha256_file(target) if target.exists() else None
        if current_hash == journal.get("after_hash"):
            try:
                load = BeancountAdapter(self.config.ledger_dir).load(self.config.root_path)
            except KernelError as exc:
                load = None
                ledger_ok = False
                load_error = str(exc)
            else:
                ledger_ok = load.ok
                load_error = None
            if not ledger_ok:
                journal["status"] = "RECOVERY_REQUIRED"
                journal["updated_at"] = now_iso()
                journal["error"] = load_error or "ledger does not load after commit"
                self.store.save_journal(journal)
                self.store.audit_append(
                    "RecoveryRequired",
                    {"commit_id": commit_id, "reason": journal["error"]},
                )
                return {"commit_id": commit_id, "outcome": "recovery_required", "reason": journal["error"]}
            if load.revision != journal.get("after_revision"):
                journal["status"] = "RECOVERY_REQUIRED"
                journal["updated_at"] = now_iso()
                journal["error"] = "ledger revision after recovery does not match the journal"
                self.store.save_journal(journal)
                self.store.audit_append(
                    "RecoveryRequired",
                    {
                        "commit_id": commit_id,
                        "reason": journal["error"],
                        "expected": journal.get("after_revision"),
                        "actual": load.revision,
                    },
                )
                return {
                    "commit_id": commit_id,
                    "outcome": "recovery_required",
                    "reason": journal["error"],
                }
            journal["status"] = "COMMITTED"
            journal["recovered_at"] = now_iso()
            journal["updated_at"] = now_iso()
            self.store.save_journal(journal)
            workflow = self.store.load_draft_state(journal["draft_id"])
            workflow["state"] = "Committed"
            workflow["commit_id"] = commit_id
            workflow["committed_at"] = now_iso()
            workflow["approval"] = None
            workflow["updated_at"] = now_iso()
            self.store.save_draft_state(journal["draft_id"], workflow)
            self.store.audit_append(
                "RecoveryCompleted",
                {"commit_id": commit_id, "draft_id": journal["draft_id"], "outcome": "commit_confirmed"},
            )
            return {"commit_id": commit_id, "outcome": "commit_confirmed"}
        if current_hash is None:
            journal["status"] = "RECOVERY_REQUIRED"
            journal["updated_at"] = now_iso()
            journal["error"] = "write target is missing; cannot prove commit outcome"
            self.store.save_journal(journal)
            self.store.audit_append(
                "RecoveryRequired",
                {"commit_id": commit_id, "reason": journal["error"]},
            )
            return {
                "commit_id": commit_id,
                "outcome": "recovery_required",
                "reason": journal["error"],
            }
        if current_hash == journal.get("before_hash"):
            journal["status"] = "ABORTED"
            journal["updated_at"] = now_iso()
            self.store.save_journal(journal)
            workflow = self.store.load_draft_state(journal["draft_id"])
            if workflow.get("state") in {"Committing", "RecoveryRequired"}:
                workflow["state"] = "Approved"
                workflow["updated_at"] = now_iso()
                self.store.save_draft_state(journal["draft_id"], workflow)
            self.store.audit_append(
                "RecoveryCompleted",
                {"commit_id": commit_id, "draft_id": journal["draft_id"], "outcome": "not_applied"},
            )
            return {"commit_id": commit_id, "outcome": "not_applied"}
        journal["status"] = "RECOVERY_REQUIRED"
        journal["updated_at"] = now_iso()
        journal["error"] = "target hash matches neither before_hash nor after_hash"
        self.store.save_journal(journal)
        self.store.audit_append(
            "RecoveryRequired",
            {"commit_id": commit_id, "reason": journal["error"], "target_hash": current_hash},
        )
        return {"commit_id": commit_id, "outcome": "recovery_required", "reason": journal["error"]}

    # ------------------------------------------------------------------ audit
    def audit_tail(self, limit: int = 20) -> list[dict[str, Any]]:
        events = self.store.audit_events()
        return events[-max(1, min(limit, 500)) :]


def _lifecycle(info: AccountInfo | None) -> str:
    if info is None:
        return "not_open"
    if info.close_date is not None and info.close_date <= dt.date.today():
        return "closed"
    if info.open_date is not None and info.open_date > dt.date.today():
        return "planned"
    return "open"
