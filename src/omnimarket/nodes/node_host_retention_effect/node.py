# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the host retention node."""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_host_retention_effect.handlers.handler_host_retention import (
    HandlerHostRetention,
)
from omnimarket.nodes.node_host_retention_effect.models.model_host_retention import (
    ModelHostRetentionRequest,
    ModelHostRetentionResult,
)


class NodeHostRetentionEffect:
    contract_path = Path(__file__).with_name("contract.yaml")

    def __init__(self) -> None:
        self.handler = HandlerHostRetention()

    def handle(self, request: ModelHostRetentionRequest) -> ModelHostRetentionResult:
        return self.handler.handle(request)
