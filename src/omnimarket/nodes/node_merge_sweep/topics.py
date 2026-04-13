# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Topic constants for node_merge_sweep.

Declared in contract.yaml event_bus subscribe_topics / publish_topics.
Reference these constants in handler code — never inline topic strings.
"""

from __future__ import annotations

# Inbound — triggers a sweep run
TOPIC_MERGE_SWEEP_START = "onex.cmd.omnimarket.merge-sweep-start.v1"

# Outbound — emitted when sweep classification completes
TOPIC_MERGE_SWEEP_COMPLETED = "onex.evt.omnimarket.merge-sweep-completed.v1"

__all__: list[str] = [
    "TOPIC_MERGE_SWEEP_COMPLETED",
    "TOPIC_MERGE_SWEEP_START",
]
