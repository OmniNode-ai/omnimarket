# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed inputs and results for action and hold classification after compaction.

The caller supplies the open-loop items, the continuation as an ordered stream of
events, the call-class policy (which classes are progress for each item kind and
which are read-only) and the held scopes. The policy is data: nothing here infers
a class from the free text of a call.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Nonempty = Annotated[str, Field(min_length=1)]
Turn = Annotated[int, Field(strict=True, ge=1)]

__all__ = [
    "EnumActionOutcome",
    "EnumEventSource",
    "EnumOpenLoopKind",
    "ModelActionScoreRequest",
    "ModelActionScoreResult",
    "ModelCallClassPolicy",
    "ModelContinuationEvent",
    "ModelHeldScope",
    "ModelHoldViolation",
    "ModelHorizonOutcome",
    "ModelHorizonSummary",
    "ModelItemActionScore",
    "ModelOpenLoopItem",
]


class EnumOpenLoopKind(StrEnum):
    OPEN_CLAIM = "OPEN_CLAIM"
    OPEN_ASK = "OPEN_ASK"
    PROMISE_SENT = "PROMISE_SENT"
    DRAFT_UNSENT = "DRAFT_UNSENT"
    UNACKED_MSG = "UNACKED_MSG"


class EnumActionOutcome(StrEnum):
    ACTED = "ACTED"
    CHECKED = "CHECKED"
    PROMPTED = "PROMPTED"
    IDLE = "IDLE"
    NOT_DUE = "NOT_DUE"


class EnumEventSource(StrEnum):
    TOOL_CALL = "tool_call"
    LEDGER_ROW = "ledger_row"
    OPERATOR_PROMPT = "operator_prompt"
    ASSISTANT_TEXT = "assistant_text"


class ModelOpenLoopItem(BaseModel):
    """One open-loop item and the strings that name it in a call, row or prompt."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    key: Nonempty
    kind: EnumOpenLoopKind
    names: tuple[Nonempty, ...] = ()
    until: AwareDatetime | None = None
    held: bool = False
    waiting_on_operator: bool = False

    @model_validator(mode="after")
    def default_names(self) -> ModelOpenLoopItem:
        if not self.names:
            object.__setattr__(self, "names", (self.key,))
        if any(not name.strip() for name in self.names):
            raise ValueError("Item names must not be blank")
        return self


class ModelContinuationEvent(BaseModel):
    """One event of the continuation, in recorded order, in the assistant turn it falls in.

    ``refused`` records a replay stub's refusal of a tool call: ``by_design`` (``gh``,
    network) still counts when the class is on the list; ``malformed`` never counts.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    turn: Turn
    source: EnumEventSource
    call_class: Nonempty | None = None
    text: str
    refused: Literal["by_design", "malformed"] | None = None

    @model_validator(mode="after")
    def class_and_refusal(self) -> ModelContinuationEvent:
        classed = self.source in (EnumEventSource.TOOL_CALL, EnumEventSource.LEDGER_ROW)
        if classed and self.call_class is None:
            raise ValueError("A tool call or ledger row needs its call_class")
        if not classed and self.call_class is not None:
            raise ValueError("Only a tool call or ledger row carries a call_class")
        if self.refused is not None and self.source is not EnumEventSource.TOOL_CALL:
            raise ValueError("Only a tool call can be refused")
        return self


class ModelCallClassPolicy(BaseModel):
    """The call classes that satisfy each kind's progress predicate, and the read-only ones."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    progress_classes: dict[EnumOpenLoopKind, tuple[Nonempty, ...]]
    read_only_classes: tuple[Nonempty, ...] = ()
    hold_classes: tuple[Nonempty, ...] = ()

    @model_validator(mode="after")
    def read_only_is_never_progress(self) -> ModelCallClassPolicy:
        read_only = set(self.read_only_classes)
        for kind, classes in self.progress_classes.items():
            overlap = read_only.intersection(classes)
            if overlap:
                raise ValueError(
                    f"Read-only classes {sorted(overlap)} are on the {kind} progress list"
                )
        return self

    def progress_for(self, kind: EnumOpenLoopKind) -> frozenset[str]:
        return frozenset(self.progress_classes.get(kind, ()))


class ModelHeldScope(BaseModel):
    """A scope (a repo, a surface) under a HOLD; ``until`` absent means in force."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    scope: Nonempty
    until: AwareDatetime | None = None


class ModelActionScoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    continuation_id: Nonempty
    judged_at: AwareDatetime
    items: tuple[ModelOpenLoopItem, ...]
    events: tuple[ModelContinuationEvent, ...]
    policy: ModelCallClassPolicy
    holds: tuple[ModelHeldScope, ...] = ()
    horizons: tuple[Annotated[int, Field(strict=True, ge=1)], ...] = (10, 25, 50)
    headline_horizon: Annotated[int, Field(strict=True, ge=1)] = 25

    @model_validator(mode="after")
    def consistent(self) -> ModelActionScoreRequest:
        keys = [item.key for item in self.items]
        if len(keys) != len(set(keys)):
            raise ValueError("Item keys must be unique")
        turns = [event.turn for event in self.events]
        if turns != sorted(turns):
            raise ValueError("Events must be in recorded order (turns non-decreasing)")
        if not self.horizons or len(self.horizons) != len(set(self.horizons)):
            raise ValueError("Horizons must be non-empty and unique")
        if self.headline_horizon not in self.horizons:
            raise ValueError("The headline horizon must be one of the horizons")
        return self


class ModelHorizonOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    horizon: int
    outcome: EnumActionOutcome


class ModelItemActionScore(BaseModel):
    """One item's turns of first progress, check, prompt and mention, and its outcome per N.

    The turns are read over the whole continuation, so they never depend on N; each
    horizon only decides which of them fall inside the cutoff.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    key: str
    kind: EnumOpenLoopKind
    not_due_reason: str | None
    first_progress_turn: int | None
    first_progress_class: str | None
    progress_after_prompt: bool
    first_prompt_turn: int | None
    first_check_turn: int | None
    first_mention_turn: int | None
    outcomes: tuple[ModelHorizonOutcome, ...]

    def outcome_at(self, horizon: int) -> EnumActionOutcome:
        for entry in self.outcomes:
            if entry.horizon == horizon:
                return entry.outcome
        raise KeyError(horizon)


class ModelHorizonSummary(BaseModel):
    """Counts at one N; ``NOT_DUE`` leaves the denominator, ``CHECKED`` counts like ``IDLE``."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    horizon: int
    acted: int
    checked: int
    prompted: int
    idle: int
    not_due: int
    denominator: int
    acted_share: float | None


class ModelHoldViolation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    event_index: int
    turn: int
    scope: str
    call_class: str
    refused: Literal["by_design", "malformed"] | None


class ModelActionScoreResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    continuation_id: str
    headline_horizon: int
    headline_acted_share: float | None
    items: tuple[ModelItemActionScore, ...]
    horizons: tuple[ModelHorizonSummary, ...]
    hold_violations: tuple[ModelHoldViolation, ...]

    def summary_at(self, horizon: int) -> ModelHorizonSummary:
        for summary in self.horizons:
            if summary.horizon == horizon:
                return summary
        raise KeyError(horizon)
