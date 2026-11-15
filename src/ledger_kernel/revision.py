"""Ledger revision fingerprints.

A ledger revision is the hash of an ``engine_fingerprint`` plus the SHA-256 of
every file in the root file's include closure, sorted by normalized relative
path. It proves *content changed*; it does not prove who changed it.
"""

from __future__ import annotations

import platform
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping

import beancount

from . import __version__
from .errors import KernelError
from .util import canonical_json_bytes, sha256_bytes, sha256_file


def engine_fingerprint() -> str:
    """Fingerprint of everything that can change Beancount semantics."""
    return f"beancount-{beancount.__version__};python-{platform.python_version()};ledger-kernel-{__version__}"


def manifest_for(root: Path, files: Iterable[Path]) -> dict[str, str]:
    """Build ``{relative posix path: sha256}`` for an already-discovered closure."""
    manifest: dict[str, str] = {}
    root = root.resolve()
    for path in files:
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(root).as_posix()
        except ValueError as exc:
            raise KernelError(
                "CLOSURE_INCONSISTENT",
                f"ledger file is outside the ledger directory: {resolved}",
                {"path": str(resolved), "ledger_dir": str(root)},
            ) from exc
        if not resolved.is_file():
            raise KernelError("LEDGER_LOAD_FAILED", f"ledger file disappeared: {resolved}")
        manifest[relative] = sha256_file(resolved)
    if not manifest:
        raise KernelError("LEDGER_LOAD_FAILED", "ledger closure is empty")
    return dict(sorted(manifest.items()))


def _canonical_manifest(manifest: Mapping[str, str]) -> dict[str, str]:
    return {str(path): str(digest) for path, digest in sorted(manifest.items())}


def revision_of(manifest: Mapping[str, str], engine: str | None = None) -> str:
    payload = {
        "engine_fingerprint": engine or engine_fingerprint(),
        "files": [[path, digest] for path, digest in _canonical_manifest(manifest).items()],
    }
    return "sha256:" + sha256_bytes(canonical_json_bytes(payload))


def contract_revision(path: Path) -> str:
    if not path.exists():
        return "sha256:absent"
    try:
        return "sha256:" + sha256_file(path)
    except OSError as exc:  # pragma: no cover - permissions are environmental
        raise KernelError("CONTRACT_INVALID", f"cannot hash contracts file: {exc}") from exc


def manifest_from_json(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(digest) for key, digest in value.items()}


def manifest_size(manifest: Mapping[str, str]) -> int:
    return len(manifest)


def diff_manifests(
    previous: Mapping[str, str], current: Mapping[str, str]
) -> dict[str, list[str]]:
    previous_paths = set(previous)
    current_paths = set(current)
    changed = sorted(
        path
        for path in previous_paths & current_paths
        if str(previous[path]) != str(current[path])
    )
    return {
        "added": sorted(current_paths - previous_paths),
        "removed": sorted(previous_paths - current_paths),
        "changed": changed,
    }


def revision_short(revision: str, width: int = 12) -> str:
    raw = revision.split(":", 1)[-1]
    return raw[:width]


def amount_to_json(value: Decimal) -> str:
    return format(value, "f")
