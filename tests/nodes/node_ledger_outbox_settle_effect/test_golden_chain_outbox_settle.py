# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain: node_ledger_outbox_settle_effect settles and stops through the node entry point."""

from __future__ import annotations

from pathlib import Path

import yaml

from omnimarket.nodes.node_ledger_outbox_settle_effect.models.model_outbox_settle import (
    ModelOutboxSettleRequest,
)
from omnimarket.nodes.node_ledger_outbox_settle_effect.node import (
    NodeLedgerOutboxSettleEffect,
)

from .test_outbox_settle import NOW, REQ, Publisher, refused_request, stamp


def test_outbox_settle_golden_chain_settles_a_refused_request_through_the_node(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "ledger.md"
    ledger.write_text("", encoding="utf-8")
    refused_request(
        ledger, f"{stamp()} | STATUS | lane=a | req={REQ} | a row\n", REQ, "refused"
    )
    node = NodeLedgerOutboxSettleEffect(Publisher(["accepted"]))
    contract = yaml.safe_load(node.contract_path.read_text())
    assert contract["name"] == "node_ledger_outbox_settle_effect"
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]
    result = node.handle(ModelOutboxSettleRequest(ledger_path=str(ledger), now=NOW))
    assert (result.settled, result.remaining, result.stopped) == (1, 0, "")


def test_outbox_settle_error_chain_stops_on_an_unanswered_bus_and_keeps_the_request(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "ledger.md"
    ledger.write_text("", encoding="utf-8")
    refused_request(
        ledger, f"{stamp()} | STATUS | lane=a | req={REQ} | a row\n", REQ, "refused"
    )
    node = NodeLedgerOutboxSettleEffect(Publisher(["pending"]))
    result = node.handle(ModelOutboxSettleRequest(ledger_path=str(ledger), now=NOW))
    assert result.settled == 0
    assert result.remaining == 1
    assert "exit 75" in result.stopped
