# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input and output of the landing decision to work-ledger mapping (OMN-17427)."""

from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from omnimarket.models.landing_decision import ModelLandingDecision
from omnimarket.models.work_ledger_append import ModelWorkLedgerAppendRequest


class ModelWorkLedgerLandingMirrorRequest(BaseModel):
    """One landing-decision event: the decision and the envelope facts that identify it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision: ModelLandingDecision
    correlation_id: UUID
    emitted_at: AwareDatetime


class ModelWorkLedgerLandingMirrorResult(BaseModel):
    """The append request for a tick that changed something, else no request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request: ModelWorkLedgerAppendRequest | None = None
    row_count: int = Field(ge=0)
