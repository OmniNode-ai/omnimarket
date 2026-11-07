# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the broker load reading: what the broker says, what the probe saw, the run."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict


class ModelBrokerMetrics(BaseModel):
    """What the broker's own public metrics say, counts only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    topics: int = Field(ge=0)
    partitions: int = Field(ge=0)
    #: Reactor shards (cores the broker runs on); each holds its share of the partitions.
    shards: int = Field(ge=1)
    #: Partitions of user topics whose log end offset is zero: they never carried a record.
    empty_partitions: int = Field(ge=0)


class EnumConsumerStart(StrEnum):
    """How a probe consumer's start ended."""

    STARTED = "started"
    #: The consumer did not finish starting inside the deadline.
    TIMEOUT = "timeout"
    #: The transport could not be opened or read at all.
    UNREADABLE = "unreadable"


class ModelConsumerStart(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: EnumConsumerStart
    elapsed_ms: int | None = None
    detail: str = ""


class ModelBrokerLoadCommand(BaseModel):
    """Run the reading once; ``write`` false reads and appends nothing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    write: bool = True


class ModelBrokerLoadResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    status: Literal["completed", "failed"]
    host: str
    failure: str | None = None
    verdict: EnumReadingVerdict = EnumReadingVerdict.UNKNOWN
    outcome: EnumReadingOutcome = EnumReadingOutcome.SOURCE_UNREADABLE
    reasons: tuple[str, ...] = ()
    metrics: ModelBrokerMetrics | None = None
    consumer: ModelConsumerStart | None = None
    partitions_per_shard: int | None = None
    #: Shards the partition bar would need for today's partitions.
    shards_needed: int | None = None
    #: Partitions to remove, at the most, to be under the bar on today's shards.
    partitions_over_bar: int | None = None
    row_key: str = ""
    ledger: str = "not-written"
    row: str | None = None

    @property
    def failed(self) -> bool:
        """The read itself failed; a NOT_WORKING verdict that was read is not a failure."""
        return (
            self.status == "failed"
            or self.outcome is not EnumReadingOutcome.READ
            or self.ledger.startswith("append-failed")
        )


__all__ = [
    "EnumConsumerStart",
    "ModelBrokerLoadCommand",
    "ModelBrokerLoadResult",
    "ModelBrokerMetrics",
    "ModelConsumerStart",
]
