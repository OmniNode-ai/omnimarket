# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Topic constants for node_filesystem_crawler_effect.

Declared in contract.yaml event_bus.publish_topics / subscribe_topics.
Reference these constants in handler and config code — never inline topic
strings directly.
"""

from __future__ import annotations

TOPIC_DOCUMENT_DISCOVERED = "onex.evt.omnimemory.document-discovered.v1"
TOPIC_DOCUMENT_CHANGED = "onex.evt.omnimemory.document-changed.v1"
TOPIC_DOCUMENT_REMOVED = "onex.evt.omnimemory.document-removed.v1"
TOPIC_DOCUMENT_INDEXED = "onex.evt.omnimemory.document-indexed.v1"

__all__: list[str] = [
    "TOPIC_DOCUMENT_CHANGED",
    "TOPIC_DOCUMENT_DISCOVERED",
    "TOPIC_DOCUMENT_INDEXED",
    "TOPIC_DOCUMENT_REMOVED",
]
