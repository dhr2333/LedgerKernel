"""Plain-file persistence for drafts, validations, audit, journal and snapshots.

Nothing in ``.ledger-kernel`` is a hidden binary database. Drafts, audit and
journal entries are readable JSON; snapshots are content-addressed blobs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .config import Config
from .errors import KernelError
from .util import (
    append_jsonl,
    atomic_write_bytes,
    atomic_write_json,
    canonical_json_bytes,
    ensure_dir,
    file_size,
    new_id,
    now_iso,
    read_json,
    read_jsonl,
    sha256_bytes,
)

GENESIS_HASH = "sha256:genesis"


class Store:
    def __init__(self, config: Config):
        self.config = config
        self.root = config.meta_dir
        self.drafts_dir = self.root / "drafts"
        self.validations_dir = self.root / "validations"
        self.audit_dir = self.root / "audit"
        self.journal_dir = self.root / "journal"
        self.observations_dir = self.root / "observations"
        self.snapshots_dir = self.root / "snapshots"
        self.blobs_dir = self.snapshots_dir / "blobs"
        self.state_path = self.root / "state.json"
        self.idempotency_path = self.root / "idempotency.json"
        self.audit_path = self.audit_dir / "events.jsonl"

    def init_layout(self) -> None:
        for path in (
            self.root,
            self.drafts_dir,
            self.validations_dir,
            self.audit_dir,
            self.journal_dir,
            self.observations_dir,
            self.blobs_dir,
        ):
            ensure_dir(path)
        if not self.contracts_path.exists():
            self.contracts_path.write_text(
                "# LedgerKernel account contracts (v0.1 schema).\n"
                "# Keep this file declarative and versioned with your ledger.\n"
                "schema_version: 1\naccounts: {}\n",
                encoding="utf-8",
            )
        if not self.audit_path.exists():
            append_jsonl(
                self.audit_path,
                {
                    "event_id": new_id("evt"),
                    "event_type": "KernelInitialized",
                    "occurred_at": now_iso(),
                    "prev_hash": GENESIS_HASH,
                    "ledger_id": self.config.ledger_id,
                    "root_file": self.config.root_file,
                    "write_target": self.config.write_target,
                    "kernel_version": "0.1.0",
                    "event_hash": "",
                },
            )
            self._seal_last_event()

    @property
    def contracts_path(self) -> Path:
        return self.config.contracts_path

    # ------------------------------------------------------------------ state
    def load_state(self) -> dict[str, Any] | None:
        if not self.state_path.exists():
            return None
        return read_json(self.state_path)

    def save_state(self, state: dict[str, Any]) -> None:
        atomic_write_json(self.state_path, state)

    # ----------------------------------------------------------------- drafts
    def _draft_path(self, draft_id: str) -> Path:
        return self.drafts_dir / f"{draft_id}.json"

    def _draft_state_path(self, draft_id: str) -> Path:
        return self.drafts_dir / f"{draft_id}.state.json"

    def save_draft(self, draft: dict[str, Any]) -> None:
        atomic_write_json(self._draft_path(draft["draft_id"]), draft)

    def load_draft(self, draft_id: str) -> dict[str, Any]:
        path = self._draft_path(draft_id)
        if not path.exists():
            raise KernelError("DRAFT_NOT_FOUND", f"draft not found: {draft_id}", {"draft_id": draft_id})
        return read_json(path)

    def save_draft_state(self, draft_id: str, state: dict[str, Any]) -> None:
        atomic_write_json(self._draft_state_path(draft_id), state)

    def load_draft_state(self, draft_id: str) -> dict[str, Any]:
        path = self._draft_state_path(draft_id)
        if not path.exists():
            raise KernelError("DRAFT_NOT_FOUND", f"draft state not found: {draft_id}")
        return read_json(path)

    def list_drafts(self) -> list[dict[str, Any]]:
        drafts: list[dict[str, Any]] = []
        if not self.drafts_dir.exists():
            return drafts
        for path in sorted(self.drafts_dir.glob("*.json")):
            if path.name.endswith(".state.json"):
                continue
            try:
                draft = read_json(path)
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(draft, dict) and draft.get("draft_id") == path.stem:
                drafts.append(draft)
        return drafts

    # ------------------------------------------------------------ validations
    def save_validation(self, validation: dict[str, Any]) -> None:
        atomic_write_json(self.validations_dir / f"{validation['validation_id']}.json", validation)

    def load_validation(self, validation_id: str) -> dict[str, Any]:
        path = self.validations_dir / f"{validation_id}.json"
        if not path.exists():
            raise KernelError(
                "VALIDATION_NOT_FOUND", f"validation not found: {validation_id}", {"validation_id": validation_id}
            )
        return read_json(path)

    # ------------------------------------------------------------ idempotency
    def load_idempotency(self) -> dict[str, str]:
        if not self.idempotency_path.exists():
            return {}
        return read_json(self.idempotency_path)

    def save_idempotency(self, mapping: dict[str, str]) -> None:
        atomic_write_json(self.idempotency_path, mapping)

    # ------------------------------------------------------------------ audit
    def audit_events(self) -> list[dict[str, Any]]:
        return read_jsonl(self.audit_path)

    def audit_append(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        actor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        events = self.audit_events()
        prev_hash = events[-1]["event_hash"] if events else GENESIS_HASH
        event: dict[str, Any] = {
            "event_id": new_id("evt"),
            "event_type": event_type,
            "occurred_at": now_iso(),
            "actor": actor or {"type": "system", "id": "ledger-kernel"},
            "prev_hash": prev_hash,
        }
        event.update(payload)
        digest = sha256_bytes(canonical_json_bytes(event))
        event["event_hash"] = f"sha256:{digest}"
        append_jsonl(self.audit_path, event)
        return event

    def _seal_last_event(self) -> None:
        """Repair the placeholder hash written by ``init_layout``."""
        events = read_jsonl(self.audit_path)
        if not events:
            return
        last = events[-1]
        if last.get("event_hash"):
            return
        last.pop("event_hash", None)
        last["event_hash"] = f"sha256:{sha256_bytes(canonical_json_bytes(last))}"
        lines = [json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for event in events]
        atomic_write_bytes(self.audit_path, ("\n".join(lines) + "\n").encode("utf-8"))

    def verify_audit(self) -> dict[str, Any]:
        try:
            events = self.audit_events()
        except KernelError as exc:
            return {"ok": False, "event_count": 0, "error": exc.message}
        if not events:
            return {"ok": False, "event_count": 0, "error": "audit log is empty"}
        if events[0].get("event_type") != "KernelInitialized":
            return {
                "ok": False,
                "event_count": len(events),
                "error": "audit log does not start with KernelInitialized",
            }
        previous = GENESIS_HASH
        for index, event in enumerate(events, start=1):
            actual = event.get("event_hash")
            if not isinstance(actual, str):
                return {"ok": False, "event_count": len(events), "error": f"event {index} has no hash"}
            body = {key: value for key, value in event.items() if key != "event_hash"}
            expected = f"sha256:{sha256_bytes(canonical_json_bytes(body))}"
            if actual != expected:
                return {"ok": False, "event_count": len(events), "error": f"event {index} hash mismatch"}
            if event.get("prev_hash") != previous:
                return {"ok": False, "event_count": len(events), "error": f"event {index} chain mismatch"}
            previous = actual
        return {"ok": True, "event_count": len(events), "error": None}

    # ---------------------------------------------------------------- journal
    def journal_path(self, commit_id: str) -> Path:
        return self.journal_dir / f"{commit_id}.json"

    def save_journal(self, journal: dict[str, Any]) -> None:
        atomic_write_json(self.journal_path(journal["commit_id"]), journal)

    def load_journal(self, commit_id: str) -> dict[str, Any]:
        path = self.journal_path(commit_id)
        if not path.exists():
            raise KernelError("VALIDATION_NOT_FOUND", f"journal not found: {commit_id}")
        return read_json(path)

    def list_journals(self) -> list[dict[str, Any]]:
        journals: list[dict[str, Any]] = []
        if not self.journal_dir.exists():
            return journals
        for path in sorted(self.journal_dir.glob("*.json")):
            try:
                journal = read_json(path)
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(journal, dict) and journal.get("commit_id") == path.stem:
                journals.append(journal)
        return journals

    # ----------------------------------------------------------- observations
    def observation_path(self, observation_id: str) -> Path:
        return self.observations_dir / f"{observation_id}.json"

    def save_observation(self, observation: dict[str, Any], diff_text: str | None = None) -> None:
        atomic_write_json(self.observation_path(observation["observation_id"]), observation)
        if diff_text:
            path = self.observations_dir / f"{observation['observation_id']}.diff"
            atomic_write_bytes(path, diff_text.encode("utf-8"))

    def load_observation(self, observation_id: str) -> dict[str, Any]:
        path = self.observation_path(observation_id)
        if not path.exists():
            raise KernelError("VALIDATION_NOT_FOUND", f"observation not found: {observation_id}")
        return read_json(path)

    def list_observations(self) -> list[dict[str, Any]]:
        observations: list[dict[str, Any]] = []
        if not self.observations_dir.exists():
            return observations
        for path in sorted(self.observations_dir.glob("*.json")):
            try:
                observation = read_json(path)
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(observation, dict):
                observations.append(observation)
        observations.sort(key=lambda item: item.get("detected_at", ""), reverse=True)
        return observations

    # -------------------------------------------------------------- snapshots
    def blob_path(self, digest: str) -> Path:
        return self.blobs_dir / digest

    def put_blob(self, data: bytes) -> str:
        digest = sha256_bytes(data)
        path = self.blob_path(digest)
        if not path.exists():
            atomic_write_bytes(path, data)
        return digest

    def put_file_blob(self, path: Path) -> str:
        return self.put_blob(path.read_bytes())

    def has_blob(self, digest: str) -> bool:
        return self.blob_path(digest).exists()

    def read_blob(self, digest: str) -> bytes:
        return self.blob_path(digest).read_bytes()

    def read_blob_text(self, digest: str) -> str | None:
        path = self.blob_path(digest)
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def store_manifest_blobs(self, ledger_dir: Path, manifest: dict[str, str]) -> None:
        for relative, digest in manifest.items():
            if self.has_blob(digest):
                continue
            path = ledger_dir / relative
            if path.is_file():
                self.put_file_blob(path)
            else:  # deleted file: no current bytes to store
                continue

    def prune_blobs(self, protected: Iterable[str] = (), max_bytes: int | None = None) -> int:
        """Delete oldest unprotected blobs until the cache is under the cap."""
        limit = self.config.snapshot_max_bytes if max_bytes is None else max_bytes
        if limit <= 0 or not self.blobs_dir.exists():
            return 0
        protected_set = set(protected)
        blobs = sorted(self.blobs_dir.iterdir(), key=lambda path: path.stat().st_mtime)
        total = sum(file_size(path) for path in blobs)
        removed = 0
        for path in blobs:
            if total <= limit:
                break
            if path.name in protected_set:
                continue
            size = file_size(path)
            try:
                path.unlink()
            except OSError:
                continue
            total -= size
            removed += 1
        return removed

    def snapshot_dir(self, commit_id: str) -> Path:
        return self.snapshots_dir / commit_id
