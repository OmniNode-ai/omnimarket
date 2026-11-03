# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the tested, rolled-back overlay tree install."""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_overlay_tree_install_effect.handlers.handler_overlay_tree_install import (
    HandlerOverlayTreeInstall,
)
from omnimarket.nodes.node_overlay_tree_install_effect.models.model_overlay_tree_install import (
    ModelOverlayTreeInstallRequest,
    ModelOverlayTreeInstallResult,
)


class NodeOverlayTreeInstallEffect:
    contract_path = Path(__file__).with_name("contract.yaml")

    def __init__(self) -> None:
        self.handler = HandlerOverlayTreeInstall()

    def handle(
        self, request: ModelOverlayTreeInstallRequest
    ) -> ModelOverlayTreeInstallResult:
        return self.handler.handle(request)
