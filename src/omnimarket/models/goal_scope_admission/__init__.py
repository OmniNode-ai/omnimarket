# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Goal-scope admission models shared by the compute node and its loader."""

from omnimarket.models.goal_scope_admission.model_goal_scope_admission import (
    ModelGoalAdmission,
    ModelGoalCandidate,
    ModelGoalScopeAdmissionRequest,
    ModelGoalScopeAdmissionResult,
    ModelGoalTicketRule,
    ModelTicketFacts,
    ModelTicketScope,
)

__all__ = [
    "ModelGoalAdmission",
    "ModelGoalCandidate",
    "ModelGoalScopeAdmissionRequest",
    "ModelGoalScopeAdmissionResult",
    "ModelGoalTicketRule",
    "ModelTicketFacts",
    "ModelTicketScope",
]
