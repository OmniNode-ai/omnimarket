# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay-store kinds registered under the ``onex.backends`` entry-point group."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from importlib.metadata import entry_points
from typing import Protocol

from omnibase_spi.protocols.storage import ProtocolOverlayStore

from omnimarket.models.overlay_store.model_store_backend import ModelStoreBackendSpec

BACKENDS_GROUP = "onex.backends"


class StoreWiringRefusedError(ValueError):
    """The wiring refused: the message names the missing or unknown input, and nothing was built."""


class ProtocolStoreBackend(Protocol):
    """A registered kind: what it declares, and how it builds the store from exactly what it declared."""

    @property
    def spec(self) -> ModelStoreBackendSpec: ...

    def build(
        self, inputs: Mapping[str, str], secrets: Mapping[str, str]
    ) -> ProtocolOverlayStore: ...


class _EntryPoint(Protocol):
    name: str

    def load(self) -> Callable[[], ProtocolStoreBackend]: ...


def backends_from_entry_points(
    points: Iterable[_EntryPoint],
) -> dict[str, ProtocolStoreBackend]:
    """Instantiate each entry point's backend and key it by kind; an entry named other than its kind refuses."""
    registry: dict[str, ProtocolStoreBackend] = {}
    for point in points:
        factory = point.load()
        backend = factory()
        if backend.spec.kind != point.name:
            raise StoreWiringRefusedError(
                f"the entry point {point.name} registers a backend of kind {backend.spec.kind}"
            )
        registry[point.name] = backend
    return registry


def load_registered_backends() -> dict[str, ProtocolStoreBackend]:
    """Every backend registered under ``onex.backends`` that is an overlay-store kind."""
    return backends_from_entry_points(
        point
        for point in entry_points(group=BACKENDS_GROUP)
        if _is_store_kind(point.value)
    )


def _is_store_kind(target: str) -> bool:
    return target.startswith("omnimarket.handlers.overlay_store.backend_")


__all__ = [
    "BACKENDS_GROUP",
    "ProtocolStoreBackend",
    "StoreWiringRefusedError",
    "backends_from_entry_points",
    "load_registered_backends",
]
