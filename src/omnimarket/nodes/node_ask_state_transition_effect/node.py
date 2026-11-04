# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point; transitions use the ledger's existing writer and ordering."""

from pathlib import Path

from omnimarket.nodes.node_ask_state_transition_effect.handlers.handler_ask_state_transition import (
    HandlerAskStateTransition,
)


class NodeAskStateTransitionEffect(HandlerAskStateTransition):
    contract_path = Path(__file__).with_name("contract.yaml")
