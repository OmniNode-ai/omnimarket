# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure single-attempt cohort reporting for availability and content (OMN-18931)."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_delegation_availability_report_compute.models.model_delegation_availability_row import (
    ModelDelegationAvailabilityRow,
)
from omnimarket.nodes.node_delegation_availability_report_compute.models.model_delegation_tier_report import (
    ModelDelegationTierReport,
)


class ModelDelegationAvailabilityReport(BaseModel):
    """A fixed cohort; retried or unattempted requests remain named exclusions."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows: tuple[ModelDelegationAvailabilityRow, ...]
    tiers: tuple[ModelDelegationTierReport, ...]
    excluded_requests: tuple[UUID, ...]
