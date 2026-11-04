# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative node for the pure tracked-item fold."""

from pathlib import Path

from omnimarket.nodes.node_ask_state_compute.handlers.handler_ask_state import (
    HandlerAskState,
)


class NodeAskStateCompute(HandlerAskState):
    contract_path = Path(__file__).with_name("contract.yaml")
