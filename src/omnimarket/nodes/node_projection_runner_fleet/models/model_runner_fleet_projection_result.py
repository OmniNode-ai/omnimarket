# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed def-B output of the runner-fleet derivation (OMN-18768)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_projection_runner_fleet.models.model_runner_fleet_class_rollup import (
    ModelRunnerFleetClassRollup,
)
from omnimarket.nodes.node_projection_runner_fleet.models.model_runner_fleet_row import (
    ModelRunnerFleetRow,
)


class ModelRunnerFleetProjectionResult(BaseModel):
    """The rows to upsert, the names to tombstone, and the per-class rollup."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows: tuple[ModelRunnerFleetRow, ...] = ()
    tombstoned_runner_names: tuple[str, ...] = Field(
        default=(),
        description=(
            "Runners this host reported previously and does not report now. "
            "They are DELETED and tombstoned, never left behind as a stale "
            "'online' row -- a deregistered runner that keeps reporting online "
            "is worse than no row at all."
        ),
    )
    class_rollup: tuple[ModelRunnerFleetClassRollup, ...] = ()
