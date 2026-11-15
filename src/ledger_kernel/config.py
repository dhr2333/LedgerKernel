"""Ledger binding configuration (``.ledger-kernel/kernel.toml``)."""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__
from .errors import CONFIG_INVALID, KernelError
from .util import ensure_dir, is_within, new_id, now_iso

META_DIR = ".ledger-kernel"
CONFIG_FILENAME = "kernel.toml"
DEFAULT_APPROVAL_TTL = 30 * 60
DEFAULT_SNAPSHOT_MAX_BYTES = 512 * 1024 * 1024


@dataclass
class Config:
    ledger_dir: Path
    ledger_id: str
    root_file: str
    write_target: str
    approval_carry_forward: bool = True
    approval_ttl_seconds: int = DEFAULT_APPROVAL_TTL
    snapshot_max_bytes: int = DEFAULT_SNAPSHOT_MAX_BYTES
    created_at: str = field(default_factory=now_iso)
    schema_version: int = 1

    @property
    def meta_dir(self) -> Path:
        return self.ledger_dir / META_DIR

    @property
    def config_path(self) -> Path:
        return self.meta_dir / CONFIG_FILENAME

    @property
    def root_path(self) -> Path:
        return (self.ledger_dir / self.root_file).resolve()

    @property
    def write_target_path(self) -> Path:
        return (self.ledger_dir / self.write_target).resolve()

    @property
    def contracts_path(self) -> Path:
        return self.meta_dir / "contracts.yaml"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "ledger_id": self.ledger_id,
            "root_file": self.root_file,
            "write_target": self.write_target,
            "approval_carry_forward": self.approval_carry_forward,
            "approval_ttl_seconds": self.approval_ttl_seconds,
            "snapshot_max_bytes": self.snapshot_max_bytes,
            "created_at": self.created_at,
            "kernel_version": __version__,
        }

    def save(self) -> None:
        ensure_dir(self.meta_dir)
        lines = [
            "# LedgerKernel binding. This file is plain text and may be backed up.",
            "schema_version = 1",
            f"ledger_id = {json.dumps(self.ledger_id, ensure_ascii=False)}",
            f"root_file = {json.dumps(self.root_file, ensure_ascii=False)}",
            f"write_target = {json.dumps(self.write_target, ensure_ascii=False)}",
            f"approval_carry_forward = {'true' if self.approval_carry_forward else 'false'}",
            f"approval_ttl_seconds = {int(self.approval_ttl_seconds)}",
            f"snapshot_max_bytes = {int(self.snapshot_max_bytes)}",
            f"created_at = {json.dumps(self.created_at, ensure_ascii=False)}",
            "",
        ]
        tmp = self.config_path.with_suffix(".toml.tmp")
        tmp.write_text("\n".join(lines), encoding="utf-8")
        tmp.replace(self.config_path)

    @classmethod
    def create(
        cls,
        ledger_dir: Path,
        root_file: str,
        write_target: str,
        *,
        ledger_id: str | None = None,
        approval_carry_forward: bool = True,
        approval_ttl_seconds: int = DEFAULT_APPROVAL_TTL,
        snapshot_max_bytes: int = DEFAULT_SNAPSHOT_MAX_BYTES,
    ) -> "Config":
        ledger_dir = ledger_dir.resolve()
        root_path = (ledger_dir / root_file).resolve()
        if not is_within(ledger_dir, root_path):
            raise KernelError(CONFIG_INVALID, "root file escapes ledger directory")
        target_path = (ledger_dir / write_target).resolve()
        if not is_within(ledger_dir, target_path):
            raise KernelError(CONFIG_INVALID, "write_target escapes ledger directory")
        config = cls(
            ledger_dir=ledger_dir,
            ledger_id=ledger_id or f"lk_{new_id('x').rsplit('_', 1)[-1]}",
            root_file=Path(root_file).as_posix(),
            write_target=Path(write_target).as_posix(),
            approval_carry_forward=approval_carry_forward,
            approval_ttl_seconds=approval_ttl_seconds,
            snapshot_max_bytes=snapshot_max_bytes,
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.schema_version != 1:
            raise KernelError(CONFIG_INVALID, f"unsupported config schema_version: {self.schema_version}")
        if not self.ledger_id or not isinstance(self.ledger_id, str):
            raise KernelError(CONFIG_INVALID, "ledger_id is required")
        for name, value in (("root_file", self.root_file), ("write_target", self.write_target)):
            path = Path(value)
            if path.is_absolute() or ".." in path.parts:
                raise KernelError(CONFIG_INVALID, f"{name} must be a normalized relative path")
            if not value or value.endswith("/"):
                raise KernelError(CONFIG_INVALID, f"{name} must point to a file")
        if self.root_file == self.write_target:
            raise KernelError(CONFIG_INVALID, "write_target must not be the ledger root file")
        if self.approval_ttl_seconds <= 0:
            raise KernelError(CONFIG_INVALID, "approval_ttl_seconds must be positive")
        if self.snapshot_max_bytes < 0:
            raise KernelError(CONFIG_INVALID, "snapshot_max_bytes must be non-negative")

    @classmethod
    def load(cls, ledger_dir: Path) -> "Config":
        ledger_dir = ledger_dir.resolve()
        path = ledger_dir / META_DIR / CONFIG_FILENAME
        if not path.exists():
            raise KernelError(
                CONFIG_INVALID,
                f"no LedgerKernel binding found at {ledger_dir}; run `ledger-kernel init` first",
                {"path": str(path)},
            )
        try:
            with open(path, "rb") as handle:
                data = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise KernelError(CONFIG_INVALID, f"cannot read kernel.toml: {exc}") from exc
        required = ("ledger_id", "root_file", "write_target")
        missing = [key for key in required if key not in data]
        if missing:
            raise KernelError(CONFIG_INVALID, f"kernel.toml is missing: {', '.join(missing)}")
        config = cls(
            ledger_dir=ledger_dir,
            ledger_id=str(data["ledger_id"]),
            root_file=str(data["root_file"]),
            write_target=str(data["write_target"]),
            approval_carry_forward=bool(data.get("approval_carry_forward", True)),
            approval_ttl_seconds=int(data.get("approval_ttl_seconds", DEFAULT_APPROVAL_TTL)),
            snapshot_max_bytes=int(data.get("snapshot_max_bytes", DEFAULT_SNAPSHOT_MAX_BYTES)),
            created_at=str(data.get("created_at", now_iso())),
            schema_version=int(data.get("schema_version", 1)),
        )
        config.validate()
        return config
