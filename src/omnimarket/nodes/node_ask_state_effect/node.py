# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the ask-state effect's carry-over operation."""

from pathlib import Path

from omnimarket.nodes.node_ask_state_effect.handlers.handler_carry_over import (
    HandlerCarryOver,
)


class NodeAskStateEffect(HandlerCarryOver):
    contract_path = Path(__file__).with_name("contract.yaml")
