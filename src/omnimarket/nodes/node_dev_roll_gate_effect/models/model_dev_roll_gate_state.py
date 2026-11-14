# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The durable state of node_dev_roll_gate_effect."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator

from omnimarket.events.runtime_deployment import ModelDeployPublishCommand
from omnimarket.models.dev_roll import ModelDevRollRecord


class ModelDevRollHeldCommand(BaseModel):
    """A command held since ``held_at`` (the gate's own clock)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: ModelDeployPublishCommand
    held_at: datetime

    @field_validator("held_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("held_at must carry a UTC offset")
        return value


class ModelDevRollGateState(BaseModel):
    """Held commands, the ids the gate knows, and the batch it released last.

    ``outstanding`` lists the released commands that have no completion or rejection
    yet; the roll is in progress while it is not empty. ``succeeded`` is True once a
    completion with a success status arrived for the batch.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    held: tuple[ModelDevRollHeldCommand, ...] = ()
    seen_command_ids: tuple[str, ...] = ()
    requested_correlation_ids: tuple[str, ...] = ()
    outstanding: tuple[str, ...] = ()
    succeeded: bool = False
    last_roll: ModelDevRollRecord | None = None
