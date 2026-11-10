# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One tip-change event that was refused, kept on the decision."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_dev_roll_request_compute.models.enum_dev_roll_event_refusal import (
    EnumDevRollEventRefusal,
)


class ModelDevRollEventRefused(BaseModel):
    """The event id, its repository and the typed reason."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: UUID
    repository: str
    reason: EnumDevRollEventRefusal
