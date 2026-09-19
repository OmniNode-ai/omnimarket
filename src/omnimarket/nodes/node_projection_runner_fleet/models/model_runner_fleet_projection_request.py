# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed def-B input for the runner-fleet derivation (OMN-18768)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_projection_runner_fleet.models.model_runner_fleet_observation_wire import (
    ModelRunnerFleetObservationWire,
)


class ModelRunnerFleetProjectionRequest(BaseModel):
    """One observation, plus the fact the derivation cannot know by itself.

    ``known_runner_names`` is the set of runners already materialized for this
    host, supplied BY THE CALLER rather than looked up here. That is what keeps
    ``handle()`` a pure function of its input -- no clock, no database, no
    ambient state, so the same request always derives the same rows -- while
    still letting the derivation name the runners that have DISAPPEARED and owe
    a tombstone. The writer that owns the database resolves it and hands it in.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    observation: ModelRunnerFleetObservationWire
    known_runner_names: tuple[str, ...] = Field(
        default=(),
        description=(
            "Runner names already materialized for this host. A name in here "
            "and absent from the observation has been deregistered and is "
            "tombstoned; a name absent from BOTH is simply not this host's."
        ),
    )
