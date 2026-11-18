# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam test: node_morning_trigger_install_compute publishes its planned event."""

from __future__ import annotations

from pathlib import Path

import yaml

CONTRACT = (
    Path(__file__).resolve().parents[2]
    / "src/omnimarket/nodes/node_morning_trigger_install_compute/contract.yaml"
)


def test_contract_publishes_the_planned_event_as_its_terminal_event() -> None:
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    planned = "onex.evt.omnimarket.morning-trigger-install-planned.v1"
    assert contract["terminal_event"] == planned
    assert contract["event_bus"]["publish_topics"] == [planned]
    assert [e["topic"] for e in contract["published_events"]] == [planned]
