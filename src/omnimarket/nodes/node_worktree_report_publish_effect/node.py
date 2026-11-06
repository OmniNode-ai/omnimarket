# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative worktree report publication effect."""

from pathlib import Path

from .handlers.handler_worktree_report_publish import HandlerWorktreeReportPublish

CONTRACT = Path(__file__).with_name("contract.yaml")


class NodeWorktreeReportPublishEffect(HandlerWorktreeReportPublish):
    """Expose the publication handler through the installed node entrypoint."""

    contract_path = CONTRACT
