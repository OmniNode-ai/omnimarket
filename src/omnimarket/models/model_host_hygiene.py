# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models for the host hygiene decision compute (OMN-23707).

Pure decision inputs and outputs: the request carries every threshold and the
host facts; the decision lists one item per (host, cleanup) pair in the
request's deterministic order.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
CleanupId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$")]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EnumHostHygieneReason(StrEnum):
    DISK_PRESSURE = "disk_pressure"
    UNREAD_RUN_ANYWAY = "unread_run_anyway"
    UNREAD_SKIPPED = "unread_skipped"
    BELOW_THRESHOLD = "below_threshold"
    INTERVAL_NOT_ELAPSED = "interval_not_elapsed"


class EnumHostHygieneUnreadPolicy(StrEnum):
    DISPATCH = "dispatch"
    SKIP = "skip"


class ModelHostHygieneCleanupPolicy(_Frozen):
    cleanup_id: CleanupId
    min_used_fraction: float = Field(ge=0, le=1)
    min_interval_seconds: int = Field(gt=0)
    when_unread: EnumHostHygieneUnreadPolicy


class ModelHostHygieneReading(_Frozen):
    read_at: datetime
    used_fraction: float | None = Field(default=None, ge=0, le=1)
    unread_reason: str = ""


class ModelHostHygieneLastDispatch(_Frozen):
    cleanup_id: CleanupId
    dispatched_at: datetime


class ModelHostHygieneHostFacts(_Frozen):
    host: NonEmptyStr
    cleanup_ids: tuple[str, ...] = Field(min_length=1)
    reading: ModelHostHygieneReading | None = None
    last_dispatched: tuple[ModelHostHygieneLastDispatch, ...] = ()


class ModelHostHygieneDecisionRequest(_Frozen):
    now: datetime
    max_reading_age_seconds: int = Field(gt=0)
    cleanups: tuple[ModelHostHygieneCleanupPolicy, ...] = Field(min_length=1)
    hosts: tuple[ModelHostHygieneHostFacts, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate(self) -> ModelHostHygieneDecisionRequest:
        declared = {c.cleanup_id for c in self.cleanups}
        if len(declared) != len(self.cleanups):
            dupes = sorted(
                {
                    c.cleanup_id
                    for c in self.cleanups
                    if sum(1 for x in self.cleanups if x.cleanup_id == c.cleanup_id) > 1
                }
            )
            raise ValueError(f"duplicate cleanup id(s): {', '.join(dupes)}")
        host_names = [h.host for h in self.hosts]
        if len(set(host_names)) != len(host_names):
            dupes = sorted({h for h in host_names if host_names.count(h) > 1})
            raise ValueError(f"duplicate host(s): {', '.join(dupes)}")
        for h in self.hosts:
            for cid in h.cleanup_ids:
                if cid not in declared:
                    raise ValueError(
                        f"host {h.host!r} declares unknown cleanup id {cid!r}"
                    )
        return self


class ModelHostHygieneDecisionItem(_Frozen):
    host: NonEmptyStr
    cleanup_id: CleanupId
    due: bool
    reason: EnumHostHygieneReason
    used_fraction: float | None = None
    reading_age_seconds: int | None = None


class ModelHostHygieneDecision(_Frozen):
    decided_at: datetime
    items: tuple[ModelHostHygieneDecisionItem, ...]

    def due_items(self) -> tuple[ModelHostHygieneDecisionItem, ...]:
        return tuple(i for i in self.items if i.due)


__all__: list[str] = [
    "EnumHostHygieneReason",
    "EnumHostHygieneUnreadPolicy",
    "ModelHostHygieneCleanupPolicy",
    "ModelHostHygieneDecision",
    "ModelHostHygieneDecisionItem",
    "ModelHostHygieneDecisionRequest",
    "ModelHostHygieneHostFacts",
    "ModelHostHygieneLastDispatch",
    "ModelHostHygieneReading",
]
