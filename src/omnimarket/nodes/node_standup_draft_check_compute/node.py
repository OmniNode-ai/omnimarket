# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative compute node for standup-draft checking; the contract routes to the handler."""

from __future__ import annotations

from omnibase_core.nodes.node_compute import NodeCompute

from omnimarket.nodes.node_standup_draft_check_compute.models import (
    ModelStandupDraftCheckRequest,
    ModelStandupDraftCheckResult,
)


class NodeStandupDraftCheckCompute(
    NodeCompute[ModelStandupDraftCheckRequest, ModelStandupDraftCheckResult]
):
    """Thin shell: HandlerStandupDraftCheck, bound in contract.yaml, owns the logic."""


__all__ = ["NodeStandupDraftCheckCompute"]
