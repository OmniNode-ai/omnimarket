# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Persona storage effect node — append-only persona snapshot persistence.

Migrated from omnimemory (OMN-8298, Wave 2).
Adapters (Postgres persona) remain in omnimemory and are injected at
runtime via DI. Omnimarket owns the contract, the models, and the
entry point.
"""

from omnimarket.nodes.node_persona_storage_effect.models import (
    ModelPersonaStorageRequest,
    ModelPersonaStorageResponse,
)

__all__ = [
    "ModelPersonaStorageRequest",
    "ModelPersonaStorageResponse",
]
