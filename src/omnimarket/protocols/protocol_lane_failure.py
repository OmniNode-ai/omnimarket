# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Canonical bus boundaries for failure normalization and shared recovery."""

from typing import Protocol

from omnimarket.models.error_normalizer import (
    ModelErrorClassNormalizeRequest,
    ModelErrorClassNormalizeResult,
)
from omnimarket.models.model_lane_failure import (
    ModelLaneFailureAdmission,
    ModelLaneFailureAdmissionRequest,
    ModelLaneFailureOwnerReply,
    ModelLaneFailureOwnerRequest,
    ModelLaneRecoveryAck,
    ModelLaneRecoveryContribution,
)


class ProtocolLaneFailureGateway(Protocol):
    async def normalize(
        self, request: ModelErrorClassNormalizeRequest
    ) -> ModelErrorClassNormalizeResult: ...
    async def admit(
        self, request: ModelLaneFailureAdmissionRequest
    ) -> ModelLaneFailureAdmission: ...
    async def owner(
        self, request: ModelLaneFailureOwnerRequest
    ) -> ModelLaneFailureOwnerReply: ...
    async def contribute(
        self, request: ModelLaneRecoveryContribution
    ) -> ModelLaneRecoveryAck: ...
