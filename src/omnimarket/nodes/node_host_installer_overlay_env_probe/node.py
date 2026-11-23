# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the overlay environment probe."""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_host_installer_overlay_env_probe.handlers.handler_overlay_env_probe import (
    HandlerOverlayEnvProbe,
)
from omnimarket.nodes.node_host_installer_overlay_env_probe.models.model_overlay_env_probe import (
    ModelOverlayEnvProbeRequest,
    ModelOverlayEnvProbeResult,
)


class NodeHostInstallerOverlayEnvProbe:
    contract_path = Path(__file__).with_name("contract.yaml")

    def __init__(self) -> None:
        self.handler = HandlerOverlayEnvProbe()

    def handle(
        self, request: ModelOverlayEnvProbeRequest
    ) -> ModelOverlayEnvProbeResult:
        return self.handler.handle(request)
