# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Skill node: dispatch_engine orchestrator.

Thin router for the ``dispatch_engine`` skill: routes a dispatch request through
RSD scoring (``node_rsd_fill_compute``) then self-healing per-repo fan-out
(``node_self_healing_dispatch_orchestrator``), returning a real dispatch receipt
with concrete worker specs (OMN-13834).
"""

from omnimarket.nodes.node_skill_dispatch_engine_orchestrator.node import (
    NodeSkillDispatchEngineOrchestrator,
)

__all__ = ["NodeSkillDispatchEngineOrchestrator"]
