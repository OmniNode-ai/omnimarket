# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A bus consumer's demand and work, read from the broker it consumes.

Demand is the outstanding backlog: the group's end offset minus its committed
offset on every partition it holds, which stays above zero until the work is
done even when production stops. Work is the evidence of a completed handler:
the entry's ``evidence.completion_record`` names a topic whose end offset (the
count of completion events ever written) is the progress counter. Committed
offsets moving is not work, and neither is the number of events a consumer
received or published. A consumer with no completion topic has no work
evidence, so its demand is unreadable rather than zero. A partition the group
never committed on cannot be sized, so the read is unreadable, never zero.

The entry's ``evidence.locator`` is the group id; the broker comes from the
request (``consumer_bootstrap``), because the dev broker is a deployment fact.
"""

from typing import Protocol

from pydantic import BaseModel, ConfigDict, NonNegativeInt

from omnimarket.models.liveness.model_automation_liveness import (
    EnumAutomationEvidenceSource,
    NonEmptyStr,
)
from omnimarket.nodes.node_automation_run_observer_effect.sources.protocol_run_evidence_source import (
    ModelDaemonProgress,
    ModelEvidenceRead,
    ModelEvidenceReadContext,
)


class ModelPartitionOffsets(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: NonEmptyStr
    partition: NonNegativeInt
    end_offset: NonNegativeInt
    #: None when the group never committed on this partition.
    committed_offset: NonNegativeInt | None = None


class ModelConsumerGroupSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    partitions: tuple[ModelPartitionOffsets, ...]
    #: Events ever written to the completion topic; None without one.
    completions_total: NonNegativeInt | None = None


class ProtocolConsumerGroupReader(Protocol):
    def read(
        self, bootstrap: str, group: str, completion_topic: str | None
    ) -> ModelConsumerGroupSnapshot: ...


class SourceConsumerGroup:
    def __init__(self, reader: ProtocolConsumerGroupReader) -> None:
        self._reader = reader

    @property
    def source(self) -> EnumAutomationEvidenceSource:
        return EnumAutomationEvidenceSource.CONSUMER_GROUP

    def read(self, context: ModelEvidenceReadContext) -> ModelEvidenceRead:
        evidence = context.entry.evidence
        if evidence is None:
            return ModelEvidenceRead(
                cursor=context.cursor, unreadable="entry names no evidence"
            )
        if context.consumer_bootstrap is None:
            return ModelEvidenceRead(
                cursor=context.cursor,
                unreadable="the request names no consumer_bootstrap broker",
            )
        group = evidence.locator
        snapshot = self._reader.read(
            context.consumer_bootstrap, group, evidence.completion_record
        )
        backlog = 0
        for part in snapshot.partitions:
            if part.committed_offset is None:
                return ModelEvidenceRead(
                    cursor=context.cursor,
                    unreadable=(
                        f"group {group} has no committed offset on "
                        f"{part.topic}[{part.partition}]"
                    ),
                )
            backlog += max(part.end_offset - part.committed_offset, 0)
        if snapshot.completions_total is None:
            # No completion topic: demand has no work evidence to be judged
            # against. The counter is unknown, so progress is not claimed.
            return ModelEvidenceRead(
                progress=ModelDaemonProgress(progress_counter=0, demand_count=None),
                cursor=context.cursor,
            )
        return ModelEvidenceRead(
            progress=ModelDaemonProgress(
                progress_counter=snapshot.completions_total, demand_count=backlog
            ),
            cursor=context.cursor,
        )
