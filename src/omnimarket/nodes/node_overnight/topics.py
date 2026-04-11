# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Topic constants for node_overnight.

Declared in contract.yaml publish_topics. Reference these constants in
handler code — never inline topic strings directly. topics.py files are on
the `scripts/lint_no_hardcoded_topics.py` allowlist.

Related:
    - OMN-8375: HandlerOvernight halt conditions + overseer tick re-injection
    - OMN-8405: HandlerOvernight event bus DI + phase-start/phase-end/complete
      envelope publishing.
"""

from __future__ import annotations

TOPIC_OVERSEER_TICK = "onex.evt.omnimarket.overseer-tick.v1"
TOPIC_OVERNIGHT_PHASE_START = "onex.evt.omnimarket.overnight-phase-start.v1"
TOPIC_OVERNIGHT_PHASE_END = "onex.evt.omnimarket.overnight-phase-completed.v1"
TOPIC_OVERNIGHT_COMPLETE = "onex.evt.omnimarket.overnight-session-completed.v1"

__all__: list[str] = [
    "TOPIC_OVERNIGHT_COMPLETE",
    "TOPIC_OVERNIGHT_PHASE_END",
    "TOPIC_OVERNIGHT_PHASE_START",
    "TOPIC_OVERSEER_TICK",
]
