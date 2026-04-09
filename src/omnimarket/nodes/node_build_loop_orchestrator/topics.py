# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Topic constants for node_build_loop_orchestrator.

Declared in contract.yaml publish_topics. Reference these constants in
handler code — never inline topic strings directly.

Related:
    - OMN-8030: Overseer verifier wiring + DoD events
"""

from __future__ import annotations

TOPIC_PHASE_TRANSITION = (
    "onex.evt.omnimarket.build-loop-orchestrator-phase-transition.v1"
)
TOPIC_COMPLETED = "onex.evt.omnimarket.build-loop-orchestrator-completed.v1"
TOPIC_DOD_CHECKED = "onex.evt.build-loop.dod-checked.v1"

__all__: list[str] = [
    "TOPIC_COMPLETED",
    "TOPIC_DOD_CHECKED",
    "TOPIC_PHASE_TRANSITION",
]
