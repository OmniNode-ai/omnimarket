# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Topic constants for node_overseer_verifier.

Declared in contract.yaml event_bus section. Reference these constants in
handler code — never inline topic strings directly.

Related:
    - OMN-8031: node_overseer_verifier in omnimarket
    - OMN-8025: Overseer seam integration epic
"""

from __future__ import annotations

TOPIC_OVERSEER_VERIFY = "onex.cmd.omnimarket.overseer-verify.v1"
TOPIC_OVERSEER_VERIFIER_COMPLETED = "onex.evt.omnimarket.overseer-verifier-completed.v1"

__all__: list[str] = [
    "TOPIC_OVERSEER_VERIFIER_COMPLETED",
    "TOPIC_OVERSEER_VERIFY",
]
