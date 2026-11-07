# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of node_secret_reading_effect: resolve a consumer's key, read a surface to scan."""

from __future__ import annotations

from typing import Protocol

from omnimarket.models.model_secret_reading import (
    ModelResolvedSecret,
    ModelSecretHandle,
)


class ProtocolSecretProbeStore(Protocol):
    """Resolve one handle in the consumer's scope.

    The value stays in process inside ``ModelResolvedSecret``, excluded from serialization. An
    implementation never raises: a store it cannot reach is ``unreachable``.
    """

    def resolve(self, handle: ModelSecretHandle) -> ModelResolvedSecret: ...


class ProtocolExposureSurfaces(Protocol):
    """Read-only text of the surfaces a value must never appear on (ledger, logs)."""

    def paths(self, pattern: str) -> tuple[str, ...]:
        """The surfaces a declared pattern names now, sorted; none when it names none."""
        ...

    def read_text(self, path: str, max_bytes: int) -> str:
        """The surface's text, at most its last ``max_bytes``. A failed read raises OSError."""
        ...


__all__ = ["ProtocolExposureSurfaces", "ProtocolSecretProbeStore"]
