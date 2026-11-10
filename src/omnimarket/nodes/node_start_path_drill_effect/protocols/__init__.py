# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of the start-path drill."""

from __future__ import annotations

from typing import Protocol

from omnimarket.protocols.protocol_gate_store import ProtocolGateStore


class ProtocolDrillStoreFactory(Protocol):
    """Builds the test gate store one path is drilled on; every path gets a fresh one."""

    def __call__(self) -> ProtocolGateStore: ...


__all__ = ["ProtocolDrillStoreFactory"]
