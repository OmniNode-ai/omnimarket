# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative Stop capture entrypoint."""

from pathlib import Path

from omnimarket.nodes.node_orchestrator_promise_capture_effect.handlers.handler_promise_capture import (
    HandlerPromiseCapture,
)


class NodeOrchestratorPromiseCaptureEffect(HandlerPromiseCapture):
    contract_path = Path(__file__).with_name("contract.yaml")
