# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The topics node_dropped_work_audit_scheduled_effect declares and publishes (OMN-26205)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_dropped_work_audit_scheduled_effect.adapters.publisher import (
    owner_missing_topic,
)

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_dropped_work_audit_scheduled_effect/contract.yaml"
)


def test_terminal_event_is_the_run_event_and_is_published() -> None:
    loaded = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert loaded["terminal_event"] == "onex.evt.omnimarket.schedule-run.v1"
    assert (
        "onex.evt.omnimarket.schedule-run.v1" in loaded["event_bus"]["publish_topics"]
    )


def test_the_owner_missing_event_has_its_topic_in_the_contract() -> None:
    loaded = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert loaded["topics"]["owner_missing"] == "onex.evt.omnimarket.owner-missing.v1"
    assert (
        "onex.evt.omnimarket.owner-missing.v1" in loaded["event_bus"]["publish_topics"]
    )
    assert owner_missing_topic() == "onex.evt.omnimarket.owner-missing.v1"


def test_subscription_topic_and_operation_are_as_declared() -> None:
    loaded = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    subs = loaded["input_subscriptions"]
    assert len(subs) == 1
    assert subs[0]["topic"] == "onex.intent.platform.runtime-tick.v1"
    assert subs[0]["operation"] == "dropped_work_audit.scheduled_run"
