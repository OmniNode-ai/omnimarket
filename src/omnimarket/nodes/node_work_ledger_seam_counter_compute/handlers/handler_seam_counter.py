# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Count the work-ledger seam for a window and check conservation (OMN-21849).

A ledger row is appended to the file, travels the work-ledger topics and is folded
into the database projection. For one window this handler counts each side, emits the
typed counts, and checks

    file = projected + refused-with-reason + pending-replay

A file row that none of the three explains is unexplained and fails the verdict; it is
placed at the seam where it stopped (never reached a topic, or reached a topic and was
not projected). Every source is injected, so the same handler runs on in-memory fakes
and on Redpanda and the database.

The constructor takes only the boot-injectable ``container`` and keyword parameters
that all default, so the runtime resolver can construct it at boot. A caller running
the handler in process passes the sources, the publisher and the topic by keyword; any
not passed is resolved from the container when the window is counted, and the topic
defaults to the one the contract declares.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

import yaml

from omnimarket.nodes.node_work_ledger_seam_counter_compute.handlers.protocols import (
    ProtocolPendingReplaySource,
    ProtocolRefusalSource,
    ProtocolSeamEventPublisher,
    ProtocolSeamSources,
)
from omnimarket.nodes.node_work_ledger_seam_counter_compute.models.model_seam_counter import (
    EnumSeamLocation,
    EnumSeamVerdict,
    ModelSeamCountedEvent,
    ModelSeamCounterRequest,
    ModelSeamCounterResult,
    ModelSeamRowRef,
    ModelSeamUnexplainedRow,
)

if TYPE_CHECKING:
    from omnibase_core.container import ModelONEXContainer

_T = TypeVar("_T")

_CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"


def publish_topic_from_contract() -> str:
    """The one topic the contract declares for the count event."""
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    topics = contract["event_bus"]["publish_topics"]
    if len(topics) != 1:
        raise ValueError("the seam counter publishes exactly one topic")
    return str(topics[0])


class HandlerSeamCounter:
    """Counts the three sides of the seam and checks conservation."""

    def __init__(
        self,
        container: ModelONEXContainer | None = None,
        *,
        sources: ProtocolSeamSources | None = None,
        refusals: ProtocolRefusalSource | None = None,
        pending: ProtocolPendingReplaySource | None = None,
        publisher: ProtocolSeamEventPublisher | None = None,
        topic: str | None = None,
    ) -> None:
        self._container = container
        self._sources = sources
        self._refusals = refusals
        self._pending = pending
        self._publisher = publisher
        self._topic = topic

    def _resolve(self, given: _T | None, key: type) -> _T:
        """Return the injected dependency, or resolve it from the container."""
        if given is not None:
            return given
        if self._container is None:
            raise ValueError(
                f"HandlerSeamCounter needs {key.__name__}: inject it or pass a "
                "container that provides it"
            )
        resolved: _T = self._container.get_service(key)
        return resolved

    def handle(self, request: ModelSeamCounterRequest) -> ModelSeamCounterResult:
        window = request.window
        seam_sources = self._resolve(self._sources, ProtocolSeamSources)
        refusal_source = self._resolve(self._refusals, ProtocolRefusalSource)
        pending_source = self._resolve(self._pending, ProtocolPendingReplaySource)
        publisher = self._resolve(self._publisher, ProtocolSeamEventPublisher)
        topic = (
            self._topic if self._topic is not None else publish_topic_from_contract()
        )
        file_rows: dict[str, ModelSeamRowRef] = {}
        for row in seam_sources.file_rows(window):
            file_rows.setdefault(row.row_id, row)
        topic_ids = set(seam_sources.topic_row_ids(window))
        projected_ids = set(seam_sources.projection_row_ids(window))
        refused_ids = {r.row_id for r in refusal_source.refusals(window)}
        pending_ids = pending_source.pending_row_ids()

        projected = refused = pending = 0
        unexplained: list[ModelSeamUnexplainedRow] = []
        for row_id, row in file_rows.items():
            if row_id in projected_ids:
                projected += 1
            elif row_id in refused_ids:
                refused += 1
            elif row_id in pending_ids:
                pending += 1
            else:
                location = (
                    EnumSeamLocation.ON_TOPIC_NOT_PROJECTED
                    if row_id in topic_ids
                    else EnumSeamLocation.ABSENT_FROM_TOPIC
                )
                unexplained.append(ModelSeamUnexplainedRow(row=row, location=location))

        event = ModelSeamCountedEvent(
            correlation_id=request.correlation_id,
            ledger_id=request.ledger_id,
            window=window,
            file_count=len(file_rows),
            topic_count=len(topic_ids),
            projection_count=len(projected_ids),
            projected_count=projected,
            refused_count=refused,
            pending_count=pending,
            unexplained_count=len(unexplained),
            conserved=not unexplained,
            unexplained_row_ids=[u.row.row_id for u in unexplained],
        )
        publisher.publish(topic, event)
        return ModelSeamCounterResult(
            verdict=EnumSeamVerdict.PASS if event.conserved else EnumSeamVerdict.FAIL,
            event=event,
            unexplained=unexplained,
        )


__all__ = ["HandlerSeamCounter", "publish_topic_from_contract"]
