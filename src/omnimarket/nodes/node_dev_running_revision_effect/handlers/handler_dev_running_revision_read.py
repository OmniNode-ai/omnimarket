# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The handler of node_dev_running_revision_effect: read the facts and return the compute's reading.

It never returns a Completed after a source failure, so an unreadable fact is never published as a reading.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from omnimarket.models.running_revision.model_running_revision_policy import (
    ModelRunningRevisionPolicy,
)
from omnimarket.models.running_revision.model_running_revision_refused import (
    ModelRunningRevisionRefused,
)
from omnimarket.nodes.node_dev_running_revision_compute.handlers.handler_dev_running_revision import (
    read_running_revision,
)
from omnimarket.nodes.node_dev_running_revision_effect.models.enum_dev_running_revision_failure_reason import (
    EnumDevRunningRevisionFailureReason,
)
from omnimarket.nodes.node_dev_running_revision_effect.models.model_dev_running_revision_read import (
    ModelDevRunningRevisionReadCommand,
    ModelDevRunningRevisionReadCompleted,
    ModelDevRunningRevisionReadFailed,
)
from omnimarket.nodes.node_dev_running_revision_effect.protocols.protocol_running_revision_source import (
    ProtocolRunningRevisionSource,
    RunningRevisionSourceError,
)


class HandlerDevRunningRevisionRead:
    """Reads the running-revision facts through the source and returns the compute's reading."""

    def __init__(
        self,
        source: ProtocolRunningRevisionSource,
        policy: ModelRunningRevisionPolicy,
        clock: Callable[[], datetime],
    ) -> None:
        self._source = source
        self._policy = policy
        self._clock = clock

    async def handle(
        self, request: ModelDevRunningRevisionReadCommand
    ) -> ModelDevRunningRevisionReadCompleted | ModelDevRunningRevisionReadFailed:
        observed_at = self._clock()
        try:
            facts = self._source.read(request.correlation_id, observed_at)
        except RunningRevisionSourceError as exc:
            return ModelDevRunningRevisionReadFailed(
                correlation_id=request.correlation_id,
                observed_at=observed_at,
                reason=EnumDevRunningRevisionFailureReason.SOURCE_UNREADABLE,
                detail=exc.detail,
            )
        result = read_running_revision(facts, self._policy)
        if isinstance(result, ModelRunningRevisionRefused):
            return ModelDevRunningRevisionReadFailed(
                correlation_id=request.correlation_id,
                observed_at=observed_at,
                reason=EnumDevRunningRevisionFailureReason.FACTS_REFUSED,
                detail=result.detail,
                refused=result,
            )
        return ModelDevRunningRevisionReadCompleted(
            correlation_id=request.correlation_id,
            observed_at=observed_at,
            reading=result,
        )


__all__ = ["HandlerDevRunningRevisionRead"]
