# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Port of node_dev_deploy_reading_effect: gather the merges, receipts and probes at a time."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from omnimarket.nodes.node_dev_deploy_reading_effect.models.model_dev_deploy_reading_run import (
    ModelDevDeployFacts,
)


class ProtocolDevDeployFactSource(Protocol):
    """The facts at ``now``. An implementation never raises: a source it cannot read names why."""

    def read(self, now: datetime) -> ModelDevDeployFacts: ...


__all__ = ["ProtocolDevDeployFactSource"]
