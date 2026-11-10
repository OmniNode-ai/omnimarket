# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_stop_escalation_effect declares its escalated event as the terminal event."""

from __future__ import annotations

from pathlib import Path

import yaml

import omnimarket.nodes.node_stop_escalation_effect as node_pkg


def test_contract_declares_the_escalated_event_as_terminal() -> None:
    contract_path = Path(node_pkg.__file__).parent / "contract.yaml"
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    assert contract["terminal_event"] == "onex.evt.omnimarket.stop-escalated.v1"
    assert contract["topics"]["stop_escalated"] == contract["terminal_event"]
