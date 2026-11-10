# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Overlay, request and result of the lab run spill (OMN-23384)."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
SpillAction = Literal[
    "spill_state", "spill_all", "restore", "prune", "preserve_worktree"
]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelSpillHostRow(_Frozen):
    """One host's spill policy: the byte cap and the names that are never copied."""

    name: NonEmptyStr
    spill_byte_cap_mib: int = Field(gt=0)
    skip_names: tuple[NonEmptyStr, ...]
    skip_patterns: tuple[NonEmptyStr, ...]


class ModelLabRunSpillOverlay(_Frozen):
    state_suffix: NonEmptyStr
    hosts: tuple[ModelSpillHostRow, ...] = Field(min_length=1)


class ModelLabRunSpillRequest(_Frozen):
    """One spill action for ``host_name``.

    ``spill_state`` copies the directory ``source`` into ``spill_root``; ``spill_all`` does so for
    every state directory of the runs directory ``source``; ``restore`` copies spilled state
    from ``spill_root`` back into the runs directory ``source`` where it is absent; ``prune``
    removes spilled entries untouched for ``keep_days``; ``preserve_worktree`` writes the
    uncommitted change of the worktree ``source`` to ``spill_root``.
    """

    action: SpillAction
    host_name: NonEmptyStr
    source: NonEmptyStr
    spill_root: NonEmptyStr
    overlay_file: str = ""
    keep_days: int = Field(default=0, ge=0)
    dry_run: bool = False

    @model_validator(mode="after")
    def prune_names_its_days(self) -> Self:
        if self.action == "prune" and self.keep_days <= 0:
            raise ValueError("prune needs keep_days above 0")
        return self


class ModelLabRunSpillResult(_Frozen):
    """What one action did. ``capped`` is True when the byte cap stopped a spill."""

    action: SpillAction
    host_name: str
    files_copied: int = 0
    bytes_allocated: int = 0
    bytes_logical: int = 0
    byte_cap: int = 0
    spill_root_bytes: int = 0
    capped: bool = False
    restored: int = 0
    pruned: tuple[str, ...] = ()
    preserved: bool = False
    skipped: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
