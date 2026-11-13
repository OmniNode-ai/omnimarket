# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory source: the default of tests. It returns the facts it was given or raises its error."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from omnimarket.models.running_revision import (
    ModelRunningRevisionFacts,
)
from omnimarket.nodes.node_dev_running_revision_effect.protocols.protocol_running_revision_source import (
    RunningRevisionSourceError,
)


class InMemoryRunningRevisionSource:
    """Facts or an error, fixed at construction; the facts are re-stamped with the asked time and id."""

    def __init__(
        self,
        facts: ModelRunningRevisionFacts | None = None,
        error: RunningRevisionSourceError | None = None,
    ) -> None:
        if (facts is None) == (error is None):
            raise ValueError("give exactly one of facts or error")
        self._facts = facts
        self._error = error

    def read(
        self, correlation_id: UUID, observed_at: datetime
    ) -> ModelRunningRevisionFacts:
        if self._error is not None:
            raise self._error
        assert self._facts is not None
        return self._facts.model_copy(
            update={"correlation_id": correlation_id, "observed_at": observed_at}
        )
