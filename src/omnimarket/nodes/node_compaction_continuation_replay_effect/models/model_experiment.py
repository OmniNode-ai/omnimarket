# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Paired continuation experiment; boundary and scoring types have one shared owner."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.compaction_measurement.action import (
    ModelActionScoreResult,
    ModelCallClassPolicy,
    ModelHeldScope,
    ModelOpenLoopItem,
)
from omnimarket.models.compaction_measurement.boundary import ModelTranscriptCase
from omnimarket.models.compaction_measurement.ground_truth import ModelGroundTruthResult

from .model_replay import (
    ModelRecordedCall,
    ModelReplayResult,
    ModelStabilityResult,
    ModelToolCall,
)


class _Frozen(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", ser_json_bytes="base64", val_json_bytes="base64"
    )


class ModelAssistantTurn(_Frozen):
    """Only assistant output can enter a continuation; no operator turn field exists."""

    text: str = ""
    calls: tuple[ModelToolCall, ...] = ()


class ModelTurnFeedback(_Frozen):
    assistant: ModelAssistantTurn
    calls: tuple[ModelRecordedCall, ...]


class ModelContinuationInput(_Frozen):
    model_id: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    context: str
    boundary_id: str
    arm: str
    repeat_index: int = Field(ge=0, le=2)
    turn: int = Field(ge=1)
    temperature: float = Field(ge=0)
    history: tuple[ModelTurnFeedback, ...] = ()


class ModelReplayContext(_Frozen):
    case: ModelTranscriptCase
    ground_truth: ModelGroundTruthResult
    arm: Literal["stock_rerun", "mod"]
    context: str
    ledger_source: str | None = None
    frozen_files: dict[str, str] = Field(default_factory=dict)
    frozen_bytes: dict[str, bytes] = Field(default_factory=dict)
    holds: tuple[ModelHeldScope, ...] = ()
    action_items: tuple[ModelOpenLoopItem, ...] | None = None
    scratch_worktree: str | None = None

    @model_validator(mode="after")
    def same_session(self) -> ModelReplayContext:
        if self.frozen_files.keys() & self.frozen_bytes.keys():
            raise ValueError("duplicate frozen file key")
        if self.action_items is not None:
            supplied = {(i.key, i.kind.value) for i in self.action_items}
            expected = {
                (i.key, i.kind.value)
                for i in self.ground_truth.items
                if i.kind.value
                in {
                    "OPEN_CLAIM",
                    "OPEN_ASK",
                    "PROMISE_SENT",
                    "DRAFT_UNSENT",
                    "UNACKED_MSG",
                }
            }
            if supplied != expected or len(supplied) != len(self.action_items):
                raise ValueError(
                    "action item overlays must match the ground-truth open loops"
                )
        if self.case.session_id != self.ground_truth.session_id:
            raise ValueError("boundary and ground truth must name the same session")
        if self.case.boundary_at != self.ground_truth.now:
            raise ValueError("ground truth must be pinned at the boundary instant")
        return self


class ModelExperimentRequest(_Frozen):
    correlation_id: str = Field(min_length=1)
    contexts: list[ModelReplayContext] = Field(min_length=2)
    n_turns: int = Field(gt=0)
    answering_model_id: str = Field(min_length=1)
    continuation_prompt: str = Field(min_length=1)
    temperature: float | None = Field(default=None, ge=0)
    tolerance_file: str = Field(min_length=1)
    policy: ModelCallClassPolicy

    @model_validator(mode="after")
    def paired_contexts(self) -> ModelExperimentRequest:
        seen: dict[str, list[ModelReplayContext]] = {}
        for cell in self.contexts:
            seen.setdefault(cell.case.boundary_uuid, []).append(cell)
        for cells in seen.values():
            if len(cells) != 2 or {c.arm for c in cells} != {"mod", "stock_rerun"}:
                raise ValueError("each boundary needs exactly one cell of each arm")
            if (
                cells[0].case != cells[1].case
                or cells[0].ground_truth != cells[1].ground_truth
            ):
                raise ValueError(
                    "paired arms must share the pinned boundary and ground truth"
                )
            if any(
                getattr(cells[0], field) != getattr(cells[1], field)
                for field in (
                    "action_items",
                    "holds",
                    "frozen_files",
                    "frozen_bytes",
                    "ledger_source",
                )
            ):
                raise ValueError(
                    "paired arms must share the frozen environment and item state"
                )
        return self


class ModelGradedRepeat(_Frozen):
    replay: ModelReplayResult
    action_score: ModelActionScoreResult
    error: str | None = None


class ModelExperimentResult(_Frozen):
    correlation_id: str
    model_id: str
    prompt_sha256: str
    tolerance_sha256: str
    repeats: list[ModelGradedRepeat]
    stability: ModelStabilityResult
    retained_cells: list[tuple[str, str]]
