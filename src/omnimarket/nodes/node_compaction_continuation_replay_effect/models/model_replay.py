# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the sandboxed continuation replay and its repeat-stability grading."""

from __future__ import annotations

import os
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EnumToolOutcome(StrEnum):
    SCRATCH_WRITE = "SCRATCH_WRITE"
    PINNED_READ = "PINNED_READ"
    REFUSED = "REFUSED"


class EnumRefusalReason(StrEnum):
    REFUSED_BY_DESIGN = "REFUSED_BY_DESIGN"  # gh, onex
    NETWORK = "NETWORK"
    MALFORMED = "MALFORMED"
    UNPINNED_READ = "UNPINNED_READ"
    UNSUPPORTED_TOOL = "UNSUPPORTED_TOOL"
    EGRESS_BLOCKED = "EGRESS_BLOCKED"  # a stub tried to leave the sandbox


class EnumReplayStatus(StrEnum):
    COMPLETED = "COMPLETED"
    ISOLATION_BREACH = "ISOLATION_BREACH"
    UNAVAILABLE = "UNAVAILABLE"


class EnumStabilityStatus(StrEnum):
    STABLE = "STABLE"
    UNSTABLE = "UNSTABLE"
    UNAVAILABLE = (
        "UNAVAILABLE"  # fewer repeats than min_repeats: never establishes stability
    )


class ModelToolCall(BaseModel):
    """One tool call the continuation proposes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str
    command: str = ""
    path: str = ""
    row: str = ""
    item_key: str = ""


class ModelRecordedCall(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", ser_json_bytes="base64", val_json_bytes="base64"
    )

    turn: int
    call: ModelToolCall
    call_class: str
    item_key: str
    outcome: EnumToolOutcome
    refusal_reason: EnumRefusalReason | None = None
    detail: str = ""
    content: bytes | None = None
    result_input_tokens: int | None = Field(default=None, ge=0)


class ModelReplayRequest(BaseModel):
    """One repeat of one compacted context, continued for n_turns with every tool stubbed."""

    model_config = ConfigDict(
        frozen=True, extra="forbid", ser_json_bytes="base64", val_json_bytes="base64"
    )

    correlation_id: str = Field(min_length=1)
    boundary_id: str = Field(min_length=1)
    arm: str = Field(min_length=1)
    repeat_index: int = Field(ge=0)
    n_turns: int = Field(ge=1)
    scratch_dir: str = Field(min_length=1)
    ledger_source: str | None = None  # copied into scratch_dir; never written
    ledger_content: bytes | None = None  # frozen once per paired experiment
    protected_paths: list[str] = Field(
        default_factory=list
    )  # real files that must not change
    frozen_files: dict[str, str] = Field(default_factory=dict)  # path -> pinned text
    frozen_bytes: dict[str, bytes] = Field(default_factory=dict)
    scripted_turns: list[list[ModelToolCall]] = Field(default_factory=list)
    context: str = ""
    continuation_prompt: str = ""
    model_id: str = ""
    temperature: float = Field(default=0.0, ge=0)
    scratch_worktree: str | None = None  # collect `git diff HEAD` here after the turns

    @model_validator(mode="after")
    def _scratch_is_not_protected(self) -> ModelReplayRequest:
        if self.frozen_files.keys() & self.frozen_bytes.keys():
            raise ValueError("duplicate frozen file key")
        scratch = os.path.realpath(self.scratch_dir)
        for p in [
            *self.protected_paths,
            *([self.ledger_source] if self.ledger_source else []),
        ]:
            real = os.path.realpath(p)
            if real == scratch or real.startswith(scratch + os.sep):
                raise ValueError(f"scratch_dir contains protected path {p}")
        return self


class ModelReplayResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumReplayStatus
    boundary_id: str
    arm: str
    repeat_index: int
    turns_run: int
    calls: list[ModelRecordedCall]
    refusal_log: list[ModelRecordedCall]
    proposed_call_set: list[str]  # sorted unique "call_class|item_key"
    scratch_ledger_path: str
    scratch_rows: list[str]
    isolation_violations: list[str]
    diff: str | None = None
    assistant_text: list[str] = Field(default_factory=list)
    failure_reason: str | None = None


class ModelStabilityCell(BaseModel):
    """The repeats of one context in one arm at one boundary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    boundary_id: str = Field(min_length=1)
    arm: str = Field(min_length=1)
    repeats: list[list[str]]  # each: a proposed_call_set


class ModelStabilityRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    cells: list[ModelStabilityCell]
    tolerance_file: str | None = (
        None  # data file; the node's own starting file when absent
    )

    @model_validator(mode="after")
    def unique_cells(self) -> ModelStabilityRequest:
        keys = [(c.boundary_id, c.arm) for c in self.cells]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate boundary/arm cell")
        return self


class ModelCellGrade(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    boundary_id: str
    arm: str
    status: EnumStabilityStatus
    min_pairwise_jaccard: float | None
    retained: bool
    removal_reason: str | None = None  # UNSTABLE_CELL | MATCHED_REMOVAL | ORPHAN_CELL


class ModelStabilityResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tolerance: float
    cells: list[ModelCellGrade]
    unstable_rate: float  # UNSTABLE cells over all cells
    unstable_rate_by_arm: dict[str, float]
    exclusion_rate_by_arm: dict[
        str, float
    ]  # removed cells (any reason) over the arm's cells
    removed_boundaries: list[str]
    report: str
