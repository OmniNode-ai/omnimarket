# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure single-attempt cohort reporting for availability and content (OMN-18931)."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from omnibase_core.enums.enum_delegation_content_verdict import (
    EnumDelegationContentVerdict,
)
from omnibase_core.enums.enum_delegation_operational_outcome import (
    EnumDelegationOperationalOutcome,
)
from pydantic import BaseModel, ConfigDict

from omnimarket.enums.enum_usage_source import EnumUsageSource

AvailabilityOutcome = EnumDelegationOperationalOutcome | Literal["no_terminal"]


class ModelDelegationAvailabilityRow(BaseModel):
    """Availability and any evaluated content, with unchanged usage provenance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    backend_tier: str
    availability: AvailabilityOutcome
    content_verdict: EnumDelegationContentVerdict | None
    quality_score: float | None
    usage_source: EnumUsageSource
    estimation_method: str | None
    source_payload_hash: str
