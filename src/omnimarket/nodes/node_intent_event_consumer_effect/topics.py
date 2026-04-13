# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Topic constants for node_intent_event_consumer_effect."""

from __future__ import annotations

TOPIC_INTENT_CLASSIFIED = "onex.evt.omniintelligence.intent-classified.v1"
TOPIC_INTENT_STORED = "onex.evt.omnimemory.intent-stored.v1"
TOPIC_INTENT_CLASSIFIED_DLQ = "onex.evt.omniintelligence.intent-classified-dlq.v1"

__all__: list[str] = [
    "TOPIC_INTENT_CLASSIFIED",
    "TOPIC_INTENT_CLASSIFIED_DLQ",
    "TOPIC_INTENT_STORED",
]
