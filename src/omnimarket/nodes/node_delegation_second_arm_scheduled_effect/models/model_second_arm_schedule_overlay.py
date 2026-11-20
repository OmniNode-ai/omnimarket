# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay the second benchmark arm scheduled effect reads: the arm's pin and the one task each tick sends."""

from __future__ import annotations

from pydantic import ConfigDict, Field

from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleDeclaration,
)


class ModelSecondArmScheduleOverlay(ModelScheduleDeclaration):
    """The arm's backend id, the task each tick sends, and the delegate command that sends it.

    ``transport_args`` carry the bus, lane and locus flags the deployed lane takes, so the overlay, not the code, names
    where the run goes. ``timeout_seconds`` bounds the one delegate command.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend_id: str = Field(min_length=1)
    task_type: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    delegate_command: tuple[str, ...] = Field(min_length=1)
    transport_args: tuple[str, ...] = ()
    timeout_seconds: int = Field(default=600, gt=0)


__all__: list[str] = [
    "ModelSecondArmScheduleOverlay",
]
