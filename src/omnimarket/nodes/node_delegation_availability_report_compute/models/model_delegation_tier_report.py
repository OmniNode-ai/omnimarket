# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure single-attempt cohort reporting for availability and content (OMN-18931)."""

from __future__ import annotations

from omnibase_core.enums.enum_delegation_content_verdict import (
    EnumDelegationContentVerdict,
)
from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_delegation_availability_report_compute.models.model_delegation_availability_row import (
    AvailabilityOutcome,
)


class ModelDelegationTierReport(BaseModel):
    """Independent availability and correctness denominators for one tier."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend_tier: str
    availability_total: int
    availability_failures: int
    availability_counts: dict[AvailabilityOutcome, int]
    correctness_total: int
    correctness_counts: dict[EnumDelegationContentVerdict, int]
