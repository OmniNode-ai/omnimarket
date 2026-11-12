# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed command, classes and result of the broker topic removal (OMN-23697).

The command says dry run or execute; the result carries the topics by class with their partitions and
the bytes produced, the candidates a removal would take, every refusal, and the partitions the broker
would hold afterwards. A dry run never carries a removed name.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class EnumRemovalMode(StrEnum):
    DRY_RUN = "dry_run"
    EXECUTE = "execute"


class EnumTopicClass(StrEnum):
    """Where a topic stands against the contracts and the broker's own offsets."""

    DECLARED_PRODUCED = "declared_produced"
    DECLARED_NEVER_PRODUCED = "declared_never_produced"
    UNDECLARED_PRODUCED = "undeclared_produced"
    UNDECLARED_NEVER_PRODUCED = "undeclared_never_produced"


class ModelTopicRemovalCommand(BaseModel):
    """Plan a removal, or carry one out; ``execute`` needs an approval reference."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    mode: EnumRemovalMode = EnumRemovalMode.DRY_RUN
    #: The operator's go for an execute run (a ledger row id); never read for a dry run.
    approval_ref: str | None = None


class ModelTopicObservation(BaseModel):
    """What the broker says about one topic, besides the listing's own evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    partitions: int = Field(ge=1)
    #: Sum of the partitions' high watermarks; ``None`` when the offsets were not read.
    high_watermark_total: int | None
    #: Consumer groups with a committed offset or membership on the topic; ``None`` when unread.
    consumer_groups: tuple[str, ...] | None
    #: Bytes produced to the topic since the broker started (the public metrics carry no
    #: on-disk size); ``None`` when unread.
    produced_bytes: int | None = Field(default=None, ge=0)


class ModelClassCount(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    topic_class: EnumTopicClass
    topics: int = Field(ge=0)
    partitions: int = Field(ge=0)
    produced_bytes: int = Field(ge=0)


class ModelRemovalCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    partitions: int
    topic_class: EnumTopicClass
    evidence: tuple[str, ...]


class ModelRefusalCount(BaseModel):
    """How many topics one refusal reason kept off the candidate list."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: str
    topics: int = Field(ge=0)
    partitions: int = Field(ge=0)


class ModelTopicRemovalResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    mode: EnumRemovalMode
    #: ``planned`` a dry run, ``removed`` an execute run that issued its deletes, ``refused`` neither.
    status: Literal["planned", "removed", "refused"]
    refusal_reasons: tuple[str, ...] = ()
    topics_before: int = 0
    partitions_before: int = 0
    classes: tuple[ModelClassCount, ...] = ()
    candidates: tuple[ModelRemovalCandidate, ...] = ()
    refused: tuple[ModelRefusalCount, ...] = ()
    candidate_partitions: int = 0
    #: Partitions the broker would hold once every candidate is gone.
    partitions_after: int = 0
    max_topics_per_run: int = 0
    over_run_cap: bool = False
    #: Names the broker confirmed removed; empty for a dry run and for a refusal.
    removed: tuple[str, ...] = ()
    deletes_issued: int = 0

    @property
    def failed(self) -> bool:
        """A refusal is a failed run; a dry run that planned is not."""
        return self.status == "refused"


__all__ = [
    "EnumRemovalMode",
    "EnumTopicClass",
    "ModelClassCount",
    "ModelRefusalCount",
    "ModelRemovalCandidate",
    "ModelTopicObservation",
    "ModelTopicRemovalCommand",
    "ModelTopicRemovalResult",
]
