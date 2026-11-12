# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The command and results of the dev running-revision read."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_dev_running_revision_compute.models.model_running_revision_reading import (
    ModelRunningRevisionReading,
)
from omnimarket.nodes.node_dev_running_revision_compute.models.model_running_revision_refused import (
    ModelRunningRevisionRefused,
)
from omnimarket.nodes.node_dev_running_revision_effect.models.enum_dev_running_revision_failure_reason import (
    EnumDevRunningRevisionFailureReason,
)


class ModelDevRunningRevisionReadCommand(BaseModel):
    """One reading tick: the correlation id that ties the command to its result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID


class ModelDevRunningRevisionReadCompleted(BaseModel):
    """The read succeeded: the reading, its observed time and the correlation id."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    observed_at: datetime
    reading: ModelRunningRevisionReading


class ModelDevRunningRevisionReadFailed(BaseModel):
    """The read failed: the reason, its detail and, when the facts were refused, the refusal."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    observed_at: datetime
    reason: EnumDevRunningRevisionFailureReason
    detail: str
    refused: ModelRunningRevisionRefused | None = None


__all__ = [
    "ModelDevRunningRevisionReadCommand",
    "ModelDevRunningRevisionReadCompleted",
    "ModelDevRunningRevisionReadFailed",
]
