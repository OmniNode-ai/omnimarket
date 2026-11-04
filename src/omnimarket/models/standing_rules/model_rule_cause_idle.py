# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared R-C/R-D crossings; item folding and dispatch remain their owners' jobs."""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from omnimarket.models.model_backlog_item import ModelBacklogItem


class ModelStandingRuleSource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    stamp: AwareDatetime
    lane: str = Field(min_length=1)


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelCanonicalCause(_Model):
    repository: str = Field(pattern=r"^[^@=\s]+$")
    class_key: str = Field(pattern=r"^[^@=\s]+$")
    base: str = Field(pattern=r"^[^@=\s]+$")

    @property
    def key(self) -> str:
        return f"cause={self.repository.casefold()}@{self.class_key}@{self.base}"


class ModelCauseReservation(_Model):
    key: str = Field(min_length=1)
    lane: str = Field(min_length=1)
    request_id: str = Field(min_length=1)


class ModelCauseReservationResult(_Model):
    inserted: bool
    holder: ModelCauseReservation


class ModelCauseRule(_Model):
    id: str = Field(min_length=1)
    check: Literal["cause_unique"]
    source_ruling: ModelStandingRuleSource
    dispatch_kinds: tuple[Literal["agent", "workflow", "runner", "lab"], ...]
    fix: str = Field(min_length=1)


class ModelProcessFixRule(_Model):
    id: str = Field(min_length=1)
    check: Literal["process_fill"]
    source_ruling: ModelStandingRuleSource
    dispatch_kinds: tuple[Literal["agent", "workflow", "runner", "lab"], ...]
    item_kinds: tuple[str, ...] = Field(min_length=1)
    ruling_kind: str = Field(min_length=1)
    closed_states: tuple[str, ...] = Field(min_length=1)
    item_fold_path: str = Field(min_length=1)
    fix: str = Field(min_length=1)


class ModelProcessFixItem(_Model):
    item_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    ruling_kind: str | None = None
    state: str = Field(min_length=1)
    running_lane: str | None = None
    work: ModelBacklogItem


class ModelProcessFixInput(_Model):
    rule: ModelProcessFixRule
    items: tuple[ModelProcessFixItem, ...]


class ModelProcessFixQueueNotice(_Model):
    item_id: str
    position: int = Field(ge=1)
    reason: str


class ModelAcceptedItemStart(_Model):
    """A persisted, accepted CLAIM in this turn, never a tool invocation."""

    lane: str = Field(min_length=1)
    for_item: str | None = None
    ask: str | None = None


class ModelProcessFixStopRequest(_Model):
    process_fixes: ModelProcessFixInput
    free_slots: int = Field(ge=0)
    accepted_starts: tuple[ModelAcceptedItemStart, ...]


class ModelProcessFixStopResult(_Model):
    refuse: bool
    required: int = Field(ge=0)
    bound_starts: int = Field(ge=0)
    reason: str = ""


class ModelProcessFixReadRequest(_Model):
    request_id: str = Field(min_length=1)
    now: AwareDatetime
    turn_started_at: AwareDatetime | None = None


class ModelProcessFixReadResult(_Model):
    process_fixes: ModelProcessFixInput
    accepted_starts: tuple[ModelAcceptedItemStart, ...] = ()
