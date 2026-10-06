# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The orchestrator took a handoff request into its workflow (OMN-20636)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class ModelPrHandoffAccepted(BaseModel):
    """REQUESTED to WAITING: the request is valid and the wait budget runs from now.

    Published once per request. It promises an answer, not a handoff: the
    request ends in exactly one :class:`ModelPrHandoffHandedOff` or
    :class:`ModelPrHandoffFailed` with the same ``correlation_id``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    handoff_key: str = Field(..., min_length=3)
    repo: str
    pr_number: int = Field(..., ge=1)
    lane: str
    expected_head_sha: str
    accepted_at: Annotated[datetime, AwareDatetime]
    deadline_at: Annotated[datetime, AwareDatetime]


__all__: list[str] = ["ModelPrHandoffAccepted"]
