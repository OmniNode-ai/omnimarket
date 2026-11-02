# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result for the overlay tree install node."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelOverlayTreeInstallRequest(BaseModel):
    """One install, verify or smoke of an overlay tree.

    ``install`` builds ``subdir`` of ``ref`` (a branch, tag or commit) from ``clone`` whole, checks it equals the commit,
    smokes it, swaps it into ``install_path`` by rename, checks again and rolls back on any failure. ``verify`` smokes
    the installed tree and checks it equals the revision ``record`` holds for it. ``smoke`` runs the smoke alone.
    ``installed_plugins`` is an ``installed_plugins.json`` whose entry for ``install_path`` takes the commit as its
    ``gitCommitSha``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["install", "verify", "smoke"]
    install_path: str
    clone: str = ""
    ref: str = ""
    subdir: str = "plugins/onex-overlays"
    plugin_name: str = "onex-overlays"
    record: str = ""
    installed_plugins: str = ""
    keep: int = Field(default=3, ge=1)
    dry_run: bool = False


class ModelOverlayCheck(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    ok: bool
    detail: str = ""


class ModelOverlayTreeInstallResult(BaseModel):
    """``verdict``: ``pass``, ``refused`` (the staged tree failed; nothing was installed), ``rolled-back`` (the installed
    tree failed and the previous one is back), ``failed`` (it failed and no tree could be put back), ``fail`` (a smoke or
    verify that found a fault) or ``dry-run``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: str
    verdict: str
    install_path: str
    commit: str = ""
    checks: tuple[ModelOverlayCheck, ...] = ()
    kept: str = ""
    restored: str = ""
    detail: str = ""
