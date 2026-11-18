# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The terminal event node_ticket_closer_scheduled_effect declares is a topic it publishes (OMN-24138)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_ticket_closer_scheduled_effect/contract.yaml"
)


def test_terminal_event_is_the_declared_output_and_is_published() -> None:
    loaded = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert loaded["terminal_event"] == "onex.evt.omnimarket.ticket-closer-result.v1"
    assert (
        "onex.evt.omnimarket.ticket-closer-result.v1"
        in loaded["event_bus"]["publish_topics"]
    )


def test_input_tick_topic_is_subscribed() -> None:
    loaded = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert (
        "onex.intent.platform.runtime-tick.v1"
        in loaded["event_bus"]["subscribe_topics"]
    )
