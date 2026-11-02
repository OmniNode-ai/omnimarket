# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Brief gate, PR-opening check: the ``Opens PR in`` line and the shared WIP refusal.

``HandlerLaneBriefPrGate.handle(ModelLaneBriefPrGateRequest) -> ModelLaneBriefPrGateResult``; the
mode and the WIP policy are injected from the ``lane_brief_pr_gate`` and ``wip_limits`` overlays.
"""

from omnimarket.nodes.node_lane_brief_pr_gate_compute.handlers.handler_lane_brief_pr_gate import (
    HandlerLaneBriefPrGate,
)

__all__ = ["HandlerLaneBriefPrGate"]
