# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_envelope_example_get_effect publishes to the topic its contract declares."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_envelope_example_get_effect"
    / "contract.yaml"
)


def test_built_event_topic_is_declared_once_and_is_the_terminal_event() -> None:
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    built = "onex.evt.omnimarket.envelope-example-get-built.v1"
    assert contract["event_bus"]["publish_topics"] == [built]
    assert contract["terminal_event"] == built
    assert contract["runtime_dispatch"]["terminal_events"]["success"] == built
    assert [event["topic"] for event in contract["published_events"]] == [built]
