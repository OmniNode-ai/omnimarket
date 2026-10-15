# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains for the watchdog effect node (OMN-20806).

The chain is the one the contract declares: a deliver command arrives on the
bus as JSON, the handler commits the episodes and delivers them, and the
delivery events leave on the seam topic the contract publishes.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_automation_liveness_watchdog_effect.handlers.handler_automation_liveness_watchdog_effect import (
    HandlerAutomationLivenessWatchdogEffect,
)
from omnimarket.nodes.node_automation_liveness_watchdog_effect.handlers.outbox_store import (
    OutboxStore,
)
from omnimarket.nodes.node_automation_liveness_watchdog_effect.models.model_watchdog_delivery import (
    ModelWatchdogDeliveryRequest,
    ModelWatchdogDeliveryResult,
)
from tests.nodes.node_automation_liveness_watchdog_effect.support import (
    FakeChannel,
    FakeEscalation,
    RecordingSink,
    high_alarm,
)

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_automation_liveness_watchdog_effect/contract.yaml"
)
COMMAND_TOPIC = "onex.cmd.omnimarket.automation-liveness-watchdog-deliver-requested.v1"
DELIVERED_TOPIC = "onex.evt.omnimarket.automation-liveness-alarm-delivered.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.automation-liveness-watchdog-delivered.v1"


def test_golden_chain_deliver_command_to_declared_topics(tmp_path: Path) -> None:
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert set(contract["event_bus"]["publish_topics"]) == {
        DELIVERED_TOPIC,
        TERMINAL_TOPIC,
    }
    assert contract["terminal_event"] == TERMINAL_TOPIC

    alarm = high_alarm()
    channel = FakeChannel()
    sink = RecordingSink()
    handler = HandlerAutomationLivenessWatchdogEffect(
        sender=channel, history=channel, escalation=FakeEscalation(), sink=sink
    )
    command = ModelWatchdogDeliveryRequest.model_validate_json(
        ModelWatchdogDeliveryRequest(
            now=alarm.opened_at,
            store_path=tmp_path / "watchdog.sqlite",
            alarms_raised=(alarm,),
            carried_state={"processes": []},
        ).model_dump_json()
    )
    result = handler.handle(command)
    assert isinstance(result, ModelWatchdogDeliveryResult)
    assert [d.delivered for d in result.delivered] == [True]
    assert result.pending == 0
    assert result.publish_failures == 0
    assert result.carried_state == {"processes": []}
    assert [topic for topic, _, _ in sink.published] == [DELIVERED_TOPIC]
    assert sink.published[0][2] == str(alarm.episode_id)

    again = handler.handle(
        ModelWatchdogDeliveryRequest(
            now=alarm.opened_at + timedelta(minutes=1),
            store_path=tmp_path / "watchdog.sqlite",
        )
    )
    assert again.delivered == ()
    assert again.carried_state == {"processes": []}
    assert len(channel.messages) == 1


def test_golden_chain_carried_state_survives_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "watchdog.sqlite"
    alarm = high_alarm()
    channel = FakeChannel()
    HandlerAutomationLivenessWatchdogEffect(
        store=OutboxStore(path), sender=channel, history=channel
    ).handle(
        ModelWatchdogDeliveryRequest(
            now=alarm.opened_at, carried_state={"evaluated_at": "t0"}
        )
    )
    restarted = OutboxStore(path)
    assert restarted.load_state() == {"evaluated_at": "t0"}


def test_error_chain_malformed_deliver_command_never_reaches_the_handler() -> None:
    with pytest.raises(ValidationError) as caught:
        ModelWatchdogDeliveryRequest.model_validate(
            {"alarms_raised": [{"episode_id": "x"}]}
        )
    assert {err["loc"][0] for err in caught.value.errors()} >= {"now", "alarms_raised"}


def test_error_chain_a_sink_that_refuses_is_counted_not_swallowed(
    tmp_path: Path,
) -> None:
    alarm = high_alarm()
    channel = FakeChannel()
    handler = HandlerAutomationLivenessWatchdogEffect(
        store=OutboxStore(tmp_path / "w.sqlite"), sender=channel, history=channel
    )
    result = handler.handle(
        ModelWatchdogDeliveryRequest(now=alarm.opened_at, alarms_raised=(alarm,))
    )
    assert result.publish_failures == 1
    assert [d.delivered for d in result.delivered] == [True]
