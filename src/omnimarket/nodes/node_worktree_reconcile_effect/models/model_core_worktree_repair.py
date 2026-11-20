# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request, repair and result for a canonical clone's stray ``core.worktree``."""

from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelCoreWorktreeBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EnumCoreWorktreeRepairKind(StrEnum):
    """The repair kinds; one so far, named in every ledger STATUS row."""

    UNSET_MISSING_CORE_WORKTREE = "unset_missing_core_worktree"


class ModelCoreWorktreeRepairRequest(ModelCoreWorktreeBase):
    """Each registry is a directory whose immediate children are canonical clones."""

    host: str = Field(min_length=1)
    registries: tuple[str, ...] = Field(min_length=1)
    # Default false: report the clones that would be repaired and change nothing.
    execute: bool = False
    lane: str = Field(default="worktree-reconcile", min_length=1)
    correlation_id: UUID


class ModelCoreWorktreeRepair(ModelCoreWorktreeBase):
    clone_path: str
    missing_directory: str
    repair_kind: EnumCoreWorktreeRepairKind
    outcome: Literal["detected", "repaired", "failed"]
    error: str | None = None
    # The ledger STATUS row of a repaired clone: its clone path, missing directory and repair kind.
    status_row: str | None = None
    # True when the writer appended the row; False when none is configured or its append failed.
    row_written: bool = False


class ModelCoreWorktreeRepairResult(ModelCoreWorktreeBase):
    host: str
    scanned: int = Field(ge=0)
    repairs: tuple[ModelCoreWorktreeRepair, ...]
    failures: int = Field(ge=0)
