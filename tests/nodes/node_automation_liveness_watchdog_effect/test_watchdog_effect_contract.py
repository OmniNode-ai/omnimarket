# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The watchdog effect's contract names an impure node and its handler (OMN-20806)."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

NODE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_automation_liveness_watchdog_effect"
)


def test_watchdog_effect_contract_routes_its_handler_and_models() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text("utf-8"))
    assert contract["node_type"] == "effect"
    assert contract["descriptor"]["purity"] == "impure"
    handler = contract["handler"]
    module = importlib.import_module(handler["module"])
    assert callable(getattr(module, handler["class"]).handle)
    for key in ("input_model", "output_model"):
        model_path, _, model_name = contract[key].rpartition(".")
        assert getattr(importlib.import_module(model_path), model_name)


def test_watchdog_effect_contract_declares_every_topic_it_uses() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text("utf-8"))
    bus = contract["event_bus"]
    assert bus["subscribe_topics"] == [contract["runtime_dispatch"]["command_topic"]]
    assert contract["terminal_event"] in bus["publish_topics"]
    assert (
        "onex.evt.omnimarket.automation-liveness-alarm-delivered.v1"
        in bus["publish_topics"]
    )
    sources = "\n".join(p.read_text("utf-8") for p in (NODE / "handlers").glob("*.py"))
    assert "onex.evt." not in sources
    assert "onex.cmd." not in sources


def test_watchdog_effect_handler_builds_without_arguments_for_auto_wiring() -> None:
    from omnimarket.nodes.node_automation_liveness_watchdog_effect.handlers.handler_automation_liveness_watchdog_effect import (
        HandlerAutomationLivenessWatchdogEffect,
    )

    assert HandlerAutomationLivenessWatchdogEffect()


def test_watchdog_effect_unwired_ports_fail_loudly_not_silently(tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta

    from omnimarket.nodes.node_automation_liveness_watchdog_effect.handlers.handler_automation_liveness_watchdog_effect import (
        HandlerAutomationLivenessWatchdogEffect,
    )
    from omnimarket.nodes.node_automation_liveness_watchdog_effect.handlers.outbox_store import (
        OutboxStore,
    )
    from omnimarket.nodes.node_automation_liveness_watchdog_effect.models.model_watchdog_delivery import (
        ModelWatchdogDeliveryRequest,
    )
    from tests.nodes.node_automation_liveness_watchdog_effect.support import (
        FakeChannel,
        high_alarm,
    )

    with pytest.raises(ValueError, match="no outbox store"):
        HandlerAutomationLivenessWatchdogEffect().handle(
            ModelWatchdogDeliveryRequest(now=datetime(2026, 10, 9, tzinfo=UTC))
        )
    # No history reader and no escalation route wired: the failed history read
    # is survived (at-least-once) and the failed escalation is an event.
    channel = FakeChannel()
    channel.refuse_with = "slack 503"
    bare = HandlerAutomationLivenessWatchdogEffect(
        store=OutboxStore(tmp_path / "w.sqlite"), sender=channel
    )
    alarm = high_alarm()
    bare.handle(
        ModelWatchdogDeliveryRequest(now=alarm.opened_at, alarms_raised=(alarm,))
    )
    later = bare.handle(
        ModelWatchdogDeliveryRequest(now=alarm.opened_at + timedelta(seconds=180))
    )
    assert later.escalated == ()
    assert later.delivered[-1].delivered is False
    assert "no external escalation route is wired" in (
        later.delivered[-1].failure or ""
    )
