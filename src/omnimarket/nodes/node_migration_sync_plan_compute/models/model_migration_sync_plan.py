# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of planning one migration-sync run (OMN-20687)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from omnimarket.models.migration_sync import (
    ModelMigrationSyncAction,
    ModelMigrationSyncInventory,
    ModelMigrationSyncLine,
)

_FROZEN = ConfigDict(frozen=True, extra="forbid")

MigrationSyncVerdict = Literal[
    "in_sync",
    "updated",
    "drift",
    "source_unresolvable",
    "skipped_unresolvable",
    "inventory_failed",
]


class ModelMigrationSyncPlanRequest(BaseModel):
    """An inventory and the mode to decide it under.

    ``check`` decides drift and changes nothing; ``write`` also plans the copies and
    removals that clear it. ``skip_unresolvable`` is the old script's explicit
    operator opt-in to exit 0 in check mode when no omnimarket source tree exists.
    """

    model_config = _FROZEN

    mode: Literal["check", "write"]
    skip_unresolvable: bool = False
    inventory: ModelMigrationSyncInventory


class ModelMigrationSyncPlanResult(BaseModel):
    """The run's decision: what the old script printed and exited with, and its changes.

    ``lines`` carry the old script's own text on its own streams, so a caller prints
    them unchanged. ``actions`` are applied in order. In write mode a run that ends
    in drift (exit 1) still carries the copies the old script had already made and no
    removals, because it exited before reaching them. ``changed_count`` is the count
    in the old script's ``done: N file(s) updated`` line.
    """

    model_config = _FROZEN

    exit_code: int
    verdict: MigrationSyncVerdict
    drift: bool = False
    lines: tuple[ModelMigrationSyncLine, ...] = ()
    actions: tuple[ModelMigrationSyncAction, ...] = ()
    changed_count: int = 0
