# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Protocols of the probe's seams: the visibility source and the broker admin client."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from omnimarket.nodes.node_bus_login_exposure_effect.models.model_bus_login_exposure import (
    ModelBusVisibilityRead,
)


class ProtocolBusVisibilitySource(Protocol):
    def read(self, *, now: datetime) -> ModelBusVisibilityRead: ...


class ProtocolBrokerAdmin(Protocol):
    """The two read-only admin calls the Redpanda source makes, as the login sees the broker."""

    def visible_topics(self, timeout: float) -> tuple[str, ...]: ...

    def visible_groups(self, timeout: float) -> tuple[str, ...]: ...


__all__ = ["ProtocolBrokerAdmin", "ProtocolBusVisibilitySource"]
