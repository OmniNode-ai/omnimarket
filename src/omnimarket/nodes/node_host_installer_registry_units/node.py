# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the registry module tick unit installer."""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
)
from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelRegistryUnitsRequest,
    ModelRegistryUnitsResult,
)


class NodeHostInstallerRegistryUnits:
    contract_path = Path(__file__).with_name("contract.yaml")

    def __init__(self) -> None:
        self.handler = HandlerRegistryUnits()

    def handle(self, request: ModelRegistryUnitsRequest) -> ModelRegistryUnitsResult:
        return self.handler.handle(request)
