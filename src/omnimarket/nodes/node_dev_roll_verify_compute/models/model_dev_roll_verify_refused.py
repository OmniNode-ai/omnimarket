# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A facts record refused as a whole, with a typed reason."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_dev_roll_verify_compute.models.enum_dev_roll_verify_refusal import (
    EnumDevRollVerifyRefusal,
)


class ModelDevRollVerifyRefused(BaseModel):
    """Published on the refused topic; no verdict is made from these facts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    job_id: str
    reason: EnumDevRollVerifyRefusal
    detail: str
