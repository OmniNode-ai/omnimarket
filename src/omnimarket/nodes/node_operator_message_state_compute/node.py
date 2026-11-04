# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entrypoint; the contract supplies routing and policy."""

from pathlib import Path

from omnimarket.nodes.node_operator_message_state_compute.handlers.handler_operator_message_state import (
    HandlerOperatorMessageState,
)


class NodeOperatorMessageStateCompute(HandlerOperatorMessageState):
    """Ledger arrivals and replies in; accountable message states out."""

    contract_path = Path(__file__).with_name("contract.yaml")
