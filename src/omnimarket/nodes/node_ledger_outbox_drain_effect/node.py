# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the bus-outbox drain."""

from pathlib import Path

from .handlers.handler_outbox_drain import HandlerOutboxDrain


class NodeLedgerOutboxDrainEffect(HandlerOutboxDrain):
    """Declarative entry point for the bus-outbox drain."""

    contract_path = Path(__file__).with_name("contract.yaml")
