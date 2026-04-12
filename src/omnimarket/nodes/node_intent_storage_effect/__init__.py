# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Intent Storage Effect node — persists classified intents to Memgraph.

Migrated from omnimemory to omnimarket (OMN-8300).
The Memgraph adapter (adapters/) remains in omnimemory.
"""

from omnimemory.nodes.node_intent_storage_effect.adapters import (
    HandlerIntentStorageAdapter,
)

from omnimarket.nodes.node_intent_storage_effect.models import (
    ModelIntentStorageRequest,
    ModelIntentStorageResponse,
)

__all__ = [
    "HandlerIntentStorageAdapter",
    "ModelIntentStorageRequest",
    "ModelIntentStorageResponse",
]
