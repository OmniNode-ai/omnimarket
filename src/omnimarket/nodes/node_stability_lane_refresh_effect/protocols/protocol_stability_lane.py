# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of node_stability_lane_refresh_effect: the pin source, the applied-set record, the runner and the health probe."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from omnimarket.nodes.node_stability_lane_refresh_effect.models.model_stability_lane_refresh import (
    ModelLaneHealthReading,
    ModelReleasePinSet,
    ModelRunOutcome,
)


class ProtocolPinSource(Protocol):
    """The committed pin file's text. Raises ``OSError`` when it cannot be read."""

    def read_text(self) -> str: ...


class ProtocolAppliedPinStore(Protocol):
    """The set the lane last refreshed to successfully, as this node recorded it."""

    def read(self) -> ModelReleasePinSet | None: ...

    def write(self, applied: ModelReleasePinSet) -> None: ...

    def render_pin_file(self, text: str) -> str:
        """Write a pin file for a bump in flight, in this store's directory, and return its path."""
        ...


class ProtocolRefreshRunner(Protocol):
    """Runs the stability refresh. Every ``docker compose up`` of the lane happens inside it."""

    def run(
        self, argv: Sequence[str], env: Mapping[str, str], timeout_seconds: int
    ) -> ModelRunOutcome: ...


class ProtocolLaneHealth(Protocol):
    """Reads the lane's /health at its overlay address. Raises ``OSError`` when it does not answer."""

    def read(self) -> ModelLaneHealthReading: ...


class ProtocolReleaseTagCheck(Protocol):
    """Whether a tag exists in the host's clone of a repository."""

    def has_tag(self, repo: str, tag: str) -> bool: ...


class ProtocolBusRequestReader(Protocol):
    """Reads the raw request strings of the serve tick's request topic."""

    def read(self, topic: str) -> list[str]: ...


class ProtocolBusEventPublisher(Protocol):
    """Publishes the serve tick's answer events back on the bus."""

    def publish(
        self, topic: str, body_bytes: bytes, key: str | None = None
    ) -> None: ...


__all__ = [
    "ProtocolAppliedPinStore",
    "ProtocolBusEventPublisher",
    "ProtocolBusRequestReader",
    "ProtocolLaneHealth",
    "ProtocolPinSource",
    "ProtocolRefreshRunner",
    "ProtocolReleaseTagCheck",
]
