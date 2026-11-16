# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the tested, rolled-back unit tree install."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from omnimarket.nodes.node_unit_tree_install_effect.handlers.handler_unit_tree_install import (
    HandlerUnitTreeInstall,
    Runner,
)
from omnimarket.nodes.node_unit_tree_install_effect.models.model_unit_tree_install import (
    ModelUnitTreeInstallRequest,
    ModelUnitTreeInstallResult,
)
from omnimarket.nodes.node_unit_tree_install_effect.overlay import (
    ModelUnitTreeInstallOverlay,
    load_overlay,
)


class NodeUnitTreeInstallEffect:
    contract_path = Path(__file__).with_name("contract.yaml")

    def __init__(
        self,
        overlay: ModelUnitTreeInstallOverlay | None = None,
        environ: Mapping[str, str] | None = None,
        runner: Runner | None = None,
    ) -> None:
        env = os.environ if environ is None else environ
        self.handler = HandlerUnitTreeInstall(
            overlay if overlay is not None else load_overlay(env), env, runner
        )

    def handle(
        self, request: ModelUnitTreeInstallRequest
    ) -> ModelUnitTreeInstallResult:
        return self.handler.handle(request)
