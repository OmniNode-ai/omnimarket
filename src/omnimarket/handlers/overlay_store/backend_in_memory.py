# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory kind: needs no input and no secret, so a unit test wires it with nothing."""

from __future__ import annotations

from collections.abc import Mapping

from omnibase_spi.protocols.storage import ProtocolOverlayStore

from omnimarket.handlers.overlay_store.store_in_memory import InMemoryOverlayStore
from omnimarket.models.overlay_store.model_store_backend import ModelStoreBackendSpec


class BackendInMemory:
    spec = ModelStoreBackendSpec(kind="store_in_memory")

    def build(
        self, inputs: Mapping[str, str], secrets: Mapping[str, str]
    ) -> ProtocolOverlayStore:
        return InMemoryOverlayStore()


__all__ = ["BackendInMemory"]
