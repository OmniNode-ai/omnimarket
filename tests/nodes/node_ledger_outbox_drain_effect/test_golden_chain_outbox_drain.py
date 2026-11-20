# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain: node_ledger_outbox_drain_effect drains and stops through the node entry point."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_ledger_outbox_drain_effect.handlers.handler_outbox_drain import (
    outbox_pending,
)
from omnimarket.nodes.node_ledger_outbox_drain_effect.models.model_outbox_drain import (
    ModelOutboxDrainRequest,
)
from omnimarket.nodes.node_ledger_outbox_drain_effect.node import (
    NodeLedgerOutboxDrainEffect,
)

from .support import NOON, REQ, Publisher, fresh_row, queue
from .test_publisher_sender import _script

pytestmark = pytest.mark.unit


def test_golden_chain_the_node_drains_through_its_entry_point_with_the_publisher_from_the_request(
    ledger: Path, tmp_path: Path
) -> None:
    queue(ledger, fresh_row(NOON), REQ, 1)
    node = NodeLedgerOutboxDrainEffect()
    contract = yaml.safe_load(node.contract_path.read_text())
    assert contract["name"] == "node_ledger_outbox_drain_effect"
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.ledger-outbox-drain-completed.v1"
    ]
    request = ModelOutboxDrainRequest(
        ledger_path=str(ledger),
        publisher_argv=(str(_script(tmp_path, "accepted")),),
        host="h1",
    )
    result = node.handle(request)
    assert (result.replayed, result.remaining, result.stopped) == (1, 0, "")
    assert outbox_pending(ledger) == []


def test_error_chain_the_node_keeps_the_request_on_an_unanswered_bus(
    ledger: Path,
) -> None:
    queue(ledger, fresh_row(NOON), REQ, 1)
    result = NodeLedgerOutboxDrainEffect(Publisher(["pending"])).handle(
        ModelOutboxDrainRequest(ledger_path=str(ledger))
    )
    assert result.replayed == 0
    assert result.remaining == 1
    assert "exit 75" in result.stopped
