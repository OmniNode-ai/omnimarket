# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the host observability installer."""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_host_observability_install_effect.handlers.handler_observability_install import (
    HandlerObservabilityInstall,
)
from omnimarket.nodes.node_host_observability_install_effect.models.model_observability import (
    ModelObservabilityInstallRequest,
    ModelObservabilityInstallResult,
)


class NodeHostObservabilityInstallEffect:
    contract_path = Path(__file__).with_name("contract.yaml")

    def __init__(self) -> None:
        self.handler = HandlerObservabilityInstall()

    def handle(
        self, request: ModelObservabilityInstallRequest
    ) -> ModelObservabilityInstallResult:
        return self.handler.handle(request)
