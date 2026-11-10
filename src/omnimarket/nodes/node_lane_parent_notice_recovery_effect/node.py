# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the parent notice recovery node."""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_lane_parent_notice_recovery_effect.handlers.handler_notice_recovery import (
    HandlerNoticeRecovery,
)
from omnimarket.nodes.node_lane_parent_notice_recovery_effect.models.model_notice_recovery import (
    ModelNoticeConsumeRequest,
    ModelNoticeConsumeResult,
)


class NodeLaneParentNoticeRecoveryEffect:
    contract_path = Path(__file__).with_name("contract.yaml")

    def __init__(self) -> None:
        self.handler = HandlerNoticeRecovery()

    def handle(self, request: ModelNoticeConsumeRequest) -> ModelNoticeConsumeResult:
        return self.handler.handle(request)
