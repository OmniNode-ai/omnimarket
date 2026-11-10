# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of the no-producer topic list and of the join gate on a no-producer topic (OMN-23151)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_seam_reading_compute.models.model_seam_row import (
    ModelSeamRow,
)
from omnimarket.seams.models.model_seam_graph import ModelSeamGraphV1


class ModelTopicLiteralMention(BaseModel):
    """A source file outside the consuming contract that spells the topic; the caller scans for these."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str = Field(min_length=1)
    source_path: str = Field(min_length=1)


class ModelNoProducerTopicRequest(BaseModel):
    """The seam graph, the literal mentions the caller found, and the two instants the list's age is read from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    graph: ModelSeamGraphV1
    literal_mentions: tuple[ModelTopicLiteralMention, ...] = ()
    #: The topic class listed, for example the segment that marks an event topic; no code default.
    topic_segment: str = Field(min_length=1)
    #: The seam row's seam name; no code default.
    seam: str = Field(min_length=1)
    graph_read_at: datetime
    now: datetime


class ModelNoProducerTopic(BaseModel):
    """One topic consumed with no producer found, with its consumers and the graph evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str
    consumers: tuple[str, ...]
    evidence: str


class ModelNoProducerTopicList(BaseModel):
    """The list, its age and the append-only seam row that carries it to the floor."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    topics: tuple[ModelNoProducerTopic, ...]
    count: int = Field(ge=0)
    graph_read_at: datetime
    generated_at: datetime
    age_seconds: float
    row: ModelSeamRow


class ModelUpstreamTypedFailure(BaseModel):
    """What a consumer receives when the producer side of its topic failed, in place of an event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["typed_failure"]
    topic: str = Field(min_length=1)
    failure_code: str = Field(min_length=1)
    detail: str = Field(min_length=1)


class EnumJoinOutcome(StrEnum):
    STARTED = "started"
    REFUSED = "refused"


class ModelNoProducerJoinRequest(BaseModel):
    """One message that arrived on a no-producer topic, as text, for the consumer that listens on it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str = Field(min_length=1)
    consumer: str = Field(min_length=1)
    payload: str
    now: datetime


class ModelNoProducerJoinResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str
    consumer: str
    outcome: EnumJoinOutcome
    consumer_started: bool
    #: ``upstream_<code>`` for a typed failure, ``bad-payload`` or ``topic-mismatch``; None when started.
    failure_code: str | None = None
    detail: str | None = None
    decided_at: datetime


__all__ = [
    "EnumJoinOutcome",
    "ModelNoProducerJoinRequest",
    "ModelNoProducerJoinResult",
    "ModelNoProducerTopic",
    "ModelNoProducerTopicList",
    "ModelNoProducerTopicRequest",
    "ModelTopicLiteralMention",
    "ModelUpstreamTypedFailure",
]
