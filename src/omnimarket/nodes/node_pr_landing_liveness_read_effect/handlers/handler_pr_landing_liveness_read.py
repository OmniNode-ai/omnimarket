# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read one PR head's automation liveness and return the fact the landing decision reads (OMN-23529).

A made read returns ``read`` with every undeclared installer source the head adds and publishes the completed event (the golden
chain). A read that could not be made returns ``unreadable`` with why, names no file, and publishes the failed event (the error
chain): the decision then holds nothing and lists the read as not made, as the old landing controller does.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.models.landing_decision import (
    EnumLivenessRead,
    ModelLandingLivenessFacts,
)
from omnimarket.nodes.node_pr_landing_liveness_read_effect.handlers.adapter_command_reader import (
    CommandLivenessReader,
)
from omnimarket.nodes.node_pr_landing_liveness_read_effect.handlers.liveness_read_errors import (
    LivenessReadError,
)
from omnimarket.nodes.node_pr_landing_liveness_read_effect.handlers.liveness_read_ports import (
    InMemoryLivenessReader,
)
from omnimarket.nodes.node_pr_landing_liveness_read_effect.models.model_liveness_read import (
    ModelLivenessReadCompleted,
    ModelLivenessReadFailed,
    ModelLivenessReadRequest,
    ModelLivenessReadResult,
)
from omnimarket.nodes.node_pr_landing_liveness_read_effect.overlay import (
    EnumLivenessReaderBackend,
    ModelLivenessReadOverlay,
)
from omnimarket.nodes.node_pr_landing_liveness_read_effect.protocols.protocol_liveness_read import (
    ProtocolLivenessReader,
    ProtocolLivenessReadPublisher,
)


class HandlerPrLandingLivenessRead:
    """Run the liveness reader for one PR head and publish what it found."""

    def __init__(
        self,
        overlay: ModelLivenessReadOverlay,
        reader: ProtocolLivenessReader,
        publisher: ProtocolLivenessReadPublisher,
    ) -> None:
        self._overlay = overlay
        self._reader = reader
        self._publisher = publisher

    def handle(self, request: ModelLivenessReadRequest) -> ModelLivenessReadResult:
        repository = request.pr.partition("#")[0]
        topics = self._overlay.topics
        try:
            sources = self._reader.read(repository, request.head_sha)
        except LivenessReadError as exc:
            detail = str(exc) or type(exc).__name__
            liveness = ModelLandingLivenessFacts(
                head_sha=request.head_sha,
                read=EnumLivenessRead.UNREADABLE,
                detail=detail,
            )
            self._publisher.publish(
                topics.failed,
                ModelLivenessReadFailed(
                    correlation_id=request.correlation_id,
                    pr=request.pr,
                    head_sha=request.head_sha,
                    detail=detail,
                ),
            )
            return ModelLivenessReadResult(
                correlation_id=request.correlation_id,
                pr=request.pr,
                liveness=liveness,
                topic=topics.failed,
            )
        liveness = ModelLandingLivenessFacts(
            head_sha=request.head_sha,
            read=EnumLivenessRead.READ,
            undeclared_sources=tuple(sources),
        )
        self._publisher.publish(
            topics.completed,
            ModelLivenessReadCompleted(
                correlation_id=request.correlation_id,
                pr=request.pr,
                liveness=liveness,
            ),
        )
        return ModelLivenessReadResult(
            correlation_id=request.correlation_id,
            pr=request.pr,
            liveness=liveness,
            topic=topics.completed,
        )


def build_reader(
    overlay: ModelLivenessReadOverlay, environ: Mapping[str, str]
) -> ProtocolLivenessReader:
    """The reader the overlay selects."""
    if overlay.reader.backend is EnumLivenessReaderBackend.COMMAND:
        return CommandLivenessReader(overlay, environ)
    return InMemoryLivenessReader({})


__all__ = ["HandlerPrLandingLivenessRead", "build_reader"]
