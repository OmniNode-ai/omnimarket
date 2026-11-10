# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of the C32 probe node: the prober that grades the boundary and the publisher that puts the run on the bus."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from omnimarket.nodes.node_c32_auth_boundary_probe_effect.models.model_c32_probe import (
    ModelC32ProbeReading,
)


class ProtocolC32Prober(Protocol):
    def run_probe(self) -> ModelC32ProbeReading: ...


class ProtocolC32ResultPublisher(Protocol):
    def publish(
        self,
        *,
        topic: str,
        event_type: str,
        payload: Mapping[str, object],
        event_id: str,
        correlation_id: str | None = None,
        partition_key: str | None = None,
    ) -> bool: ...
