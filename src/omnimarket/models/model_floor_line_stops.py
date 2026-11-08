# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Stage counts and stops of a floor line: the vocabulary the contract declares and the typed stop with its evidence.

A stop is a reason a line is not moving, with the rows or reads that show it (lean line board plan, section 4). The
thirteen stop kinds, their order and their sentences are contract config; the compute holds none of them.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.model_floor_line_select import LineClass

EvidenceKind = Literal["ledger-row", "pr-context", "source-health", "merge", "running"]


class ModelEvidenceRef(BaseModel):
    """One read behind a stop: a ledger row, a pull request check, a source health entry, a merge or a running read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EvidenceKind
    ref: str = Field(min_length=1)
    read_at: str = ""


class ModelLineStop(BaseModel):
    """A reason a line is stopped. It cannot be built without evidence: a stop nobody can check is not a stop."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(min_length=1)
    sentence: str = Field(min_length=1)
    since: str = ""
    evidence: tuple[ModelEvidenceRef, ...] = Field(min_length=1)
    # A cause row's words, or the contract's line saying none is recorded. An ``unread`` stop has no cause to give.
    cause: str = ""


class ModelStageCount(BaseModel):
    """How many of a line's work tasks the evidence puts at one stage."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stage: str = Field(min_length=1)
    count: int = Field(ge=0)


class ModelStopKindRule(BaseModel):
    """One stop kind: its name and the sentence its stop prints. ``{name}`` fields are filled from the stop's facts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(min_length=1)
    sentence: str = Field(min_length=1)


class ModelFloorLineStopsConfig(BaseModel):
    """Every value the stage and stop rules use."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # The thirteen stop kinds in the order a line's stops print.
    stop_kinds: tuple[ModelStopKindRule, ...] = Field(min_length=1)
    # The stages in order, then the stage of a task whose needed source is unread.
    stage_order: tuple[str, ...] = Field(min_length=5, max_length=5)
    unread_stage: str = Field(min_length=1)
    # The sources each class of line depends on; a source in one of these reading unread or stale is an ``unread`` stop.
    unread_sources: dict[LineClass, tuple[str, ...]]
    required_contexts_source: str = Field(min_length=1)
    pr_watcher_source: str = Field(min_length=1)
    ledger_source: str = Field(min_length=1)
    running_source: str = Field(min_length=1)
    process_did_nothing_runs: int = Field(gt=0)
    # A gap between two run records of a process up to this multiple of its interval still counts as consecutive.
    run_gap_factor: float = Field(gt=1.0)
    idle_hours: int = Field(gt=0)
    sentence_chars: int = Field(gt=0)
    cause_missing: str = Field(min_length=1)
    cause_row_kinds: tuple[str, ...] = Field(min_length=1)
    cause_cells: tuple[str, ...] = Field(min_length=1)
    failing_results: tuple[str, ...] = Field(min_length=1)
    blocked_outcomes: tuple[str, ...] = Field(min_length=1)
    partial_outcomes: tuple[str, ...] = Field(min_length=1)
    handoff_outcomes: tuple[str, ...] = Field(min_length=1)
    handoff_status_cell: str = Field(min_length=1)
    done_outcomes: tuple[str, ...] = Field(min_length=1)
    release_kind: str = Field(min_length=1)
    person_row_kinds: tuple[str, ...] = Field(min_length=1)
    ask_kind: str = Field(min_length=1)
    ask_status_kind: str = Field(min_length=1)
    ask_status_state: str = Field(min_length=1)
    answer_kinds: tuple[str, ...] = Field(min_length=1)
    head_chars: int = Field(gt=0)
    idle_ignored_classes: tuple[LineClass, ...] = ()

    @model_validator(mode="after")
    def _kinds_are_unique(self) -> ModelFloorLineStopsConfig:
        names = [rule.kind for rule in self.stop_kinds]
        if len(set(names)) != len(names):
            raise ValueError("stop_kinds names a kind twice")
        return self
