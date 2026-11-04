# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative worktree report publication effect."""

from pathlib import Path

from .handlers.handler_worktree_report_publish import HandlerWorktreeReportPublish
from .models import ModelWorktreeReportPublishRequest, ModelWorktreeReportPublishResult

CONTRACT = Path(__file__).with_name("contract.yaml")


class NodeWorktreeReportPublishEffect:
    contract_path = CONTRACT

    def handle(
        self, request: ModelWorktreeReportPublishRequest
    ) -> ModelWorktreeReportPublishResult:
        return HandlerWorktreeReportPublish().handle(request)
