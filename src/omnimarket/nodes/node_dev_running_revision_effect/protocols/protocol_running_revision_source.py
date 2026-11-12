# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The protocol the running-revision source implements: read the facts for one tick."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

from omnimarket.nodes.node_dev_running_revision_compute.models.model_running_revision_facts import (
    ModelRunningRevisionFacts,
)


class RunningRevisionSourceError(Exception):
    """A fact could not be read: the detail names what failed."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@runtime_checkable
class ProtocolRunningRevisionSource(Protocol):
    """Reads the running-revision facts for one tick; raises RunningRevisionSourceError when a fact cannot be read."""

    def read(
        self, correlation_id: UUID, observed_at: datetime
    ) -> ModelRunningRevisionFacts: ...
