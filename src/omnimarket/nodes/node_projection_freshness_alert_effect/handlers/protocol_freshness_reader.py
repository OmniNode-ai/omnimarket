# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Structural boundary for supply and independent demand aggregates."""

from typing import Protocol

from omnimarket.nodes.node_projection_freshness_alert_effect.models import (
    ModelFreshnessCheck,
    ModelFreshnessObservation,
)


class ProtocolFreshnessReader(Protocol):
    """Implemented by the Postgres boundary and hermetic test readers."""

    def read(self, check: ModelFreshnessCheck) -> ModelFreshnessObservation:
        """Read both aggregates, raising if either cannot be observed."""
        ...
