# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Structural boundary for supply and independent demand aggregates."""

from typing import Protocol

from omnimarket.nodes.node_projection_freshness_alert_effect.models import (
    ModelCoverageCheck,
    ModelCoverageObservation,
    ModelFreshnessCheck,
    ModelFreshnessObservation,
)


class ProtocolFreshnessReader(Protocol):
    """Implemented by the Postgres boundary and hermetic test readers."""

    def read(self, check: ModelFreshnessCheck) -> ModelFreshnessObservation:
        """Read both aggregates, raising if either cannot be observed."""
        ...

    def read_coverage(self, check: ModelCoverageCheck) -> ModelCoverageObservation:
        """Read done-ticket demand and unprojected tickets, raising if either cannot be observed."""
        ...
