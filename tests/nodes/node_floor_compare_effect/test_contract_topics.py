# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The topics node_floor_compare_effect declares are the topics its handler produces (OMN-23018)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_floor_compare_effect/contract.yaml"
)
WRITTEN = "onex.evt.omnimarket.floor-compare-written.v1"


def _contract() -> dict[str, object]:
    loaded: dict[str, object] = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    return loaded


def test_compare_effect_publishes_only_its_terminal_topic() -> None:
    contract = _contract()
    bus = contract["event_bus"]
    assert isinstance(bus, dict)
    assert bus["publish_topics"] == [WRITTEN]


def test_compare_effect_terminal_event_is_the_written_topic() -> None:
    assert _contract()["terminal_event"] == WRITTEN
