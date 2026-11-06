# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative promise compute entrypoint."""

from pathlib import Path

from omnimarket.nodes.node_orchestrator_promise_classify_compute.handlers.handler_promise_classify import (
    HandlerPromiseClassify,
)


class NodeOrchestratorPromiseClassifyCompute(HandlerPromiseClassify):
    contract_path = Path(__file__).with_name("contract.yaml")
