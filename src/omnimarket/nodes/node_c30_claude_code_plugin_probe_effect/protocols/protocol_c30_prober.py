# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of the C30 probe node: the prober that grades the boundary and the publisher that puts the run on the bus."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from omnimarket.nodes.node_c30_claude_code_plugin_probe_effect.models.model_c30_probe import (
    ModelC30ProbeReading,
)


class ProtocolC30Prober(Protocol):
    def run_probe(self) -> ModelC30ProbeReading: ...


class ProtocolC30ResultPublisher(Protocol):
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
