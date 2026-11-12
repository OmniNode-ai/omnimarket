# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A facts record refused as a whole, with a typed reason."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_dev_running_revision_compute.models.enum_running_revision_refusal_reason import (
    EnumRunningRevisionRefusalReason,
)


class ModelRunningRevisionRefused(BaseModel):
    """Published on the refused topic; no decision is made from these facts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    reason: EnumRunningRevisionRefusalReason
    detail: str
