# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request, plan and receipt of the additive in-place venv install."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class EnumVenvInstallStatus(StrEnum):
    NOOP = "noop"
    INSTALLED = "installed"
    ROLLED_BACK = "rolled_back"
    FAILED_BEFORE_EXCHANGE = "failed_before_exchange"


class ModelLockedPackage(BaseModel):
    """One package the lock pins; ``spec`` is what the installer is asked for."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    spec: str | None = None
    commit: str | None = None

    @property
    def requirement(self) -> str:
        return self.spec if self.spec is not None else f"{self.name}=={self.version}"


class ModelVenvInstallRequestBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelVenvAtomicInstallRequest(ModelVenvInstallRequestBase):
    """Paths come from the caller; nothing here is a host default."""

    venv_root: Path
    site_packages: Path
    staging_dir: Path
    journal_path: Path
    locked: tuple[ModelLockedPackage, ...] = ()
    lock_file: Path | None = None
    python: Path | None = None
    installer_command: tuple[str, ...] = ()


class ModelMovedPackage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    installed: str | None
    locked: str
    commit: str | None = None


class ModelVenvAtomicInstallResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: EnumVenvInstallStatus
    moved: tuple[ModelMovedPackage, ...] = ()
    unchanged: int = 0
    exchanged_units: tuple[str, ...] = ()
    restored_units: tuple[str, ...] = ()
    recovered_units: tuple[str, ...] = ()
    reason: str = ""
