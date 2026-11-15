# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result for the canonical clone sync unit installer."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelCloneSyncUnitsRequest(BaseModel):
    """Install, inspect or remove the canonical clone sync agent of one host.

    ``unit`` names a declaration in this node's contract: ``canonical-clone-sync`` (launchd) or
    ``canonical-clone-sync-systemd`` (a systemd user timer). ``registry_root`` is the registry root the tick runs from and
    ``ledger_path`` the rolling ledger its converge rows go to; neither has a default.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit: Literal["canonical-clone-sync", "canonical-clone-sync-systemd"]
    mode: Literal["dry-run", "install", "status", "uninstall", "run-now"] = "dry-run"
    home: str
    registry_root: str
    ledger_path: str
    onex_state_dir: str | None = None


class ModelCloneSyncUnitsResult(BaseModel):
    """``status`` is ``success``, ``failure`` or ``refused``; ``rendered`` holds the unit text by file name."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit: str
    mode: str
    status: str
    exit_code: int = 0
    reason: str = ""
    stdout: str = ""
    stderr: str = ""
    changed: bool = False
    rendered: dict[str, str] = Field(default_factory=dict)
