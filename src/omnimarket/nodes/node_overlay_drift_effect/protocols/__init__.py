# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of node_overlay_drift_effect: where the overlays come from and who validates them."""

from __future__ import annotations

from typing import Protocol

from omnimarket.nodes.node_overlay_drift_effect.models.model_overlay_drift import (
    ModelOverlayPin,
    ModelRuntimeValidation,
)


class OverlayHomeError(RuntimeError):
    """The home could not be read (its pins, its overlay list or one overlay file)."""


class RuntimeValidatorError(RuntimeError):
    """The runtime could not answer: no interpreter, a timeout, or an answer that is not typed."""


class ProtocolOverlayHome(Protocol):
    """Read-only view of the overlay home. A read that fails raises ``OverlayHomeError``."""

    def pins(self) -> tuple[ModelOverlayPin, ...]: ...

    def overlay_ids(self) -> tuple[str, ...]:
        """Every overlay id the home holds, sorted."""
        ...

    def text(self, overlay_id: str) -> str: ...

    def revision(self) -> str:
        """The commit the home is at, or ``unknown``."""
        ...


class ProtocolRuntimeValidator(Protocol):
    """Validate overlay texts through the models one runtime has installed."""

    def validate(
        self, entries: tuple[tuple[ModelOverlayPin, str], ...]
    ) -> ModelRuntimeValidation:
        """Each (pin, overlay text) through the pin's model; raises ``RuntimeValidatorError``."""
        ...


__all__ = [
    "OverlayHomeError",
    "ProtocolOverlayHome",
    "ProtocolRuntimeValidator",
    "RuntimeValidatorError",
]
