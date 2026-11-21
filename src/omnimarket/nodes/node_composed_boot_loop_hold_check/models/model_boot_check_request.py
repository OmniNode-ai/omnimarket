# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request of the composed-runtime boot check: a boot's observation and the bounds to judge it against."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_composed_boot_loop_hold_check.models.model_boot_bounds import (
    ModelBootBounds,
)
from omnimarket.nodes.node_composed_boot_loop_hold_check.models.model_boot_observation import (
    ModelBootObservation,
)


class ModelBootCheckRequest(BaseModel):
    """One boot's observation and the bounds it is judged against."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    bounds: ModelBootBounds
    observation: ModelBootObservation
