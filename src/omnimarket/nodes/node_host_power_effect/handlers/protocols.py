# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of the host power effect: the backend that serves a declared route."""

from __future__ import annotations

from typing import Protocol

from omnimarket.nodes.node_host_power_effect.models.model_host_power import (
    EnumHostPowerRoute,
    ModelHostPowerBackendReply,
    ModelHostPowerRequest,
)


class ProtocolHostPowerBackend(Protocol):
    """Serves the routes it supports. Never raises for a refusal or a device failure."""

    @property
    def routes(self) -> frozenset[EnumHostPowerRoute]: ...

    def apply(self, request: ModelHostPowerRequest) -> ModelHostPowerBackendReply: ...
