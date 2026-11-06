# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-backed live operator-item snapshot."""

from pathlib import Path

from .handlers.handler_operator_items_read import HandlerOperatorItemsRead


class NodeOperatorItemsReadEffect(HandlerOperatorItemsRead):
    contract_path = Path(__file__).with_name("contract.yaml")
