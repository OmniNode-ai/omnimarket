# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the bus-outbox settle."""

from pathlib import Path

from .handlers.handler_outbox_settle import HandlerOutboxSettle


class NodeLedgerOutboxSettleEffect(HandlerOutboxSettle):
    """Declarative entry point for the bus-outbox settle."""

    contract_path = Path(__file__).with_name("contract.yaml")
