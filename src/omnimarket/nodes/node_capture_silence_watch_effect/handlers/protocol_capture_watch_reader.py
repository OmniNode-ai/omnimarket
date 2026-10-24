# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Structural boundary for source and active principal aggregates."""

from datetime import datetime
from typing import Protocol

from omnimarket.nodes.node_capture_silence_watch_effect.models import (
    ModelCaptureObservation,
    ModelCaptureSource,
)


class ProtocolCaptureWatchReader(Protocol):
    """Implemented by the Postgres boundary and hermetic test readers."""

    def read(
        self, source: ModelCaptureSource, *, active_since: datetime
    ) -> ModelCaptureObservation:
        """Read both aggregates, raising if either cannot be observed."""
        ...
