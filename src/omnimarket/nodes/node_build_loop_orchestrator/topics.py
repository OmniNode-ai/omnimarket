# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Topic constants for node_build_loop_orchestrator.

Declared in contract.yaml publish_topics. Reference these constants in
handler code — never inline topic strings directly.

Related:
    - OMN-8030: Overseer verifier wiring + DoD events
    - OMN-8025: Overseer seam integration epic
"""

from __future__ import annotations

TOPIC_PHASE_TRANSITION = (
    "onex.evt.omnimarket.build-loop-orchestrator-phase-transition.v1"
)
TOPIC_COMPLETED = "onex.evt.omnimarket.build-loop-orchestrator-completed.v1"
TOPIC_DOD_CHECKED = "onex.evt.build-loop.dod-checked.v1"
# Canonical omnimarket topic names — must match node_overseer_verifier/contract.yaml
TOPIC_OVERSEER_VERIFY_REQUESTED = "onex.cmd.omnimarket.overseer-verify.v1"
TOPIC_OVERSEER_VERIFICATION_COMPLETED = (
    "onex.evt.omnimarket.overseer-verifier-completed.v1"
)

__all__: list[str] = [
    "TOPIC_COMPLETED",
    "TOPIC_DOD_CHECKED",
    "TOPIC_OVERSEER_VERIFICATION_COMPLETED",
    "TOPIC_OVERSEER_VERIFY_REQUESTED",
    "TOPIC_PHASE_TRANSITION",
]
