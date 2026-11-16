# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the host container unit installer."""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_host_installer_container_units.handlers.handler_container_units import (
    HandlerContainerUnits,
)
from omnimarket.nodes.node_host_installer_container_units.models.model_container_units import (
    ModelContainerUnitsRequest,
    ModelContainerUnitsResult,
)


class NodeHostInstallerContainerUnits:
    contract_path = Path(__file__).with_name("contract.yaml")

    def __init__(self) -> None:
        self.handler = HandlerContainerUnits()

    def handle(self, request: ModelContainerUnitsRequest) -> ModelContainerUnitsResult:
        return self.handler.handle(request)
