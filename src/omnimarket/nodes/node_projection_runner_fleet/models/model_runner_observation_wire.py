# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wire shape of one runner inside a fleet observation (OMN-18768)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_projection_runner_fleet.models.enum_runner_status import (
    EnumRunnerStatus,
)


class ModelRunnerObservationWire(BaseModel):
    """One runner as the emitter observed it.

    ``extra="ignore"`` so the emitter may add a field without breaking every
    deployed consumer; ``frozen`` because a projected observation is a fact
    about a moment and is never edited in flight.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    runner_name: str = Field(min_length=1)
    runner_id: int | None = None
    label_class: str = Field(min_length=1)
    labels: tuple[str, ...] = ()
    host: str = Field(
        min_length=1,
        description=(
            "The runner's OWN host, from its `host-<id>` label when it carries "
            "one. Distinct from the machine that took the observation."
        ),
    )
    observing_host: str | None = Field(
        default=None,
        description=(
            "The machine that took the observation. Falls back to the "
            "observation's host when the emitter did not name it per runner."
        ),
    )
    status: EnumRunnerStatus
    current_job_id: str | None = Field(
        default=None,
        description=(
            "The job this runner is executing, when the emitter could resolve "
            "it. NULL means 'executing something we could not name' on a BUSY "
            "runner and 'not executing' otherwise -- three different facts, "
            "never collapsed into one."
        ),
    )
    observed_at: datetime
