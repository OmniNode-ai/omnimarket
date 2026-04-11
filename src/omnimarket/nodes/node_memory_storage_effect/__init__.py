# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Memory storage effect node — CRUD across filesystem/PostgreSQL/Redis.

Migrated from omnimemory (OMN-8298, Wave 2).
Adapters (HandlerFileSystemAdapter, etc.) remain in omnimemory and are
injected at runtime via the DI container. Omnimarket owns the contract,
the models, and the entry point.
"""

from omnimarket.nodes.node_memory_storage_effect.models import (
    ModelMemoryStorageRequest,
    ModelMemoryStorageResponse,
)

__all__ = [
    "ModelMemoryStorageRequest",
    "ModelMemoryStorageResponse",
]
