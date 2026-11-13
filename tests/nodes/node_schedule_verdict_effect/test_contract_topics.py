# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The terminal event node_schedule_verdict_effect declares is a topic it publishes (OMN-19998)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_schedule_verdict_effect/contract.yaml"
)


def test_terminal_event_is_the_declared_output_and_is_published() -> None:
    loaded = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert loaded["terminal_event"] == "onex.evt.omnimarket.schedule-verdict.v1"
    assert (
        "onex.evt.omnimarket.schedule-verdict.v1"
        in loaded["event_bus"]["publish_topics"]
    )


def test_subscription_topic_and_operation_are_as_declared() -> None:
    loaded = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    subs = loaded["input_subscriptions"]
    assert len(subs) == 1
    assert subs[0]["topic"] == "onex.intent.platform.runtime-tick.v1"
    assert subs[0]["operation"] == "schedule_verdict.scheduled"
