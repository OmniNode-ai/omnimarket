# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dod_verify start command for one ticketed delegation run (OMN-19514)."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_dod_verify_execution_audience import (
    EnumDodVerifyExecutionAudience,
)


class ModelDelegationDodVerifyRequest(BaseModel):
    """A subset of the dod_verify start command's wire shape.

    The consumer refuses unknown keys, so this model declares only keys that
    command declares; a test validates one against it. It is declared here
    rather than imported because a node does not import another node's models.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(..., description="The ticket the delegation worked.")
    correlation_id: UUID = Field(
        ..., description="Verification run id, derived from the delegation run."
    )
    delegation_correlation_id: UUID = Field(
        ..., description="The delegation run whose output the verification judges."
    )
    execution_audience: EnumDodVerifyExecutionAudience = Field(
        ..., description="Evidence execution boundary; a delegation run is hosted."
    )


__all__ = ["ModelDelegationDodVerifyRequest"]
