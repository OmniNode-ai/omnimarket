# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point of the pure open-item renderer."""

from pathlib import Path

from omnimarket.nodes.node_operator_item_render_compute.handlers.handler_operator_item_render import (
    HandlerOperatorItemRender,
)


class NodeOperatorItemRenderCompute(HandlerOperatorItemRender):
    contract_path = Path(__file__).with_name("contract.yaml")
