# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Topic constants for node_overnight.

Declared in contract.yaml publish_topics. Reference these constants in
handler code — never inline topic strings directly.

Related:
    - OMN-8375: HandlerOvernight halt conditions + overseer tick re-injection
"""

from __future__ import annotations

TOPIC_OVERSEER_TICK = "onex.evt.overseer.tick.v1"

__all__: list[str] = [
    "TOPIC_OVERSEER_TICK",
]
