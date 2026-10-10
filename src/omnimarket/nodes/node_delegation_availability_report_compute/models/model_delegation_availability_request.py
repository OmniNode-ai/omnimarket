# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Caller-selected fixed delegation cohort for availability reporting."""

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_delegation_availability_report_compute.models.model_delegation_cohort_observation import (
    ModelDelegationCohortObservation,
)


class ModelDelegationAvailabilityRequest(BaseModel):
    """Retained observations to report without dispatch or retry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    observations: tuple[ModelDelegationCohortObservation, ...]
