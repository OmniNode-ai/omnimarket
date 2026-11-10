"""The order-advance node declares its answered and failed event topics."""

from __future__ import annotations

from pathlib import Path

import yaml

import omnimarket.nodes.node_order_advance_effect as node_pkg


def test_contract_declares_the_answered_and_failed_topics() -> None:
    contract = yaml.safe_load(
        (Path(node_pkg.__file__).parent / "contract.yaml").read_text()
    )
    assert contract["terminal_event"] == "onex.evt.omnimarket.order-advance-answered.v1"
    assert contract["topics"]["order_advance_answered"] == contract["terminal_event"]
    assert (
        contract["topics"]["order_advance_failed"]
        == "onex.evt.omnimarket.order-advance-failed.v1"
    )
