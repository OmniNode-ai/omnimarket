# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the temp-directory retention node."""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_temp_retention_effect.handlers.handler_temp_retention import (
    HandlerTempRetention,
)
from omnimarket.nodes.node_temp_retention_effect.models.model_temp_retention import (
    ModelRetentionPass,
)
from omnimarket.nodes.node_temp_retention_effect.models.model_temp_retention_request import (
    ModelTempRetentionRequest,
)


class NodeTempRetentionEffect:
    contract_path = Path(__file__).with_name("contract.yaml")

    def __init__(self) -> None:
        self.handler = HandlerTempRetention()

    def handle(self, request: ModelTempRetentionRequest) -> ModelRetentionPass:
        return self.handler.handle(request)
