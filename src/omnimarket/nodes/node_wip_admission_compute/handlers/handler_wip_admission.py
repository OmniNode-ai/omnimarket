# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Decide whether one caller may start work."""

from __future__ import annotations

from omnimarket.handlers.rules_wip_admission import decide_wip_admission
from omnimarket.models.wip_admission import (
    ModelWipAdmissionDecision,
    ModelWipAdmissionRequest,
)


class HandlerWipAdmission:
    """``handle(ModelWipAdmissionRequest) -> ModelWipAdmissionDecision``; pure, no clock read."""

    def handle(self, request: ModelWipAdmissionRequest) -> ModelWipAdmissionDecision:
        return decide_wip_admission(request)
