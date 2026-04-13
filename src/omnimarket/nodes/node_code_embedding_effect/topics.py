# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Topic constants for node_code_embedding_effect.

Declared in contract.yaml event_bus — never inline topic strings in handler code.
"""

from __future__ import annotations

TOPIC_CODE_ENTITIES_EXTRACTED = "onex.evt.omnimarket.code-entities-extracted.v1"
TOPIC_CODE_EMBEDDED = "onex.evt.omnimarket.code-embedded.v1"

__all__: list[str] = [
    "TOPIC_CODE_EMBEDDED",
    "TOPIC_CODE_ENTITIES_EXTRACTED",
]
