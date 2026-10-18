# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A silent primary raises PRIMARY_SILENT on the direct route (OMN-20807, AC1)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from omnimarket.models.liveness.model_automation_liveness import (
    EnumAutomationLivenessReason,
    EnumAutomationLivenessVerdict,
)
from omnimarket.models.liveness.model_automation_liveness_deadman import (
    EnumDeadmanDeliveryRoute,
    ModelAutomationLivenessDeadmanRequest,
)
from omnimarket.nodes.node_automation_liveness_deadman_compute.handlers.handler_automation_liveness_deadman import (
    HandlerAutomationLivenessDeadman,
)
from omnimarket.nodes.node_automation_liveness_watchdog_effect.handlers.handler_automation_liveness_watchdog_effect import (
    HandlerAutomationLivenessWatchdogEffect,
)
from omnimarket.nodes.node_automation_liveness_watchdog_effect.handlers.outbox_store import (
    OutboxStore,
)
from omnimarket.nodes.node_automation_liveness_watchdog_effect.models.model_watchdog_delivery import (
    ModelWatchdogDeliveryRequest,
)
from tests.nodes.node_automation_liveness_deadman_effect.support import (
    T0,
    RecordingSink,
)
from tests.nodes.node_automation_liveness_watchdog_effect.support import (
    FakeChannel,
    FakeEscalation,
)

pytestmark = pytest.mark.unit

_V = EnumAutomationLivenessVerdict


def _request(**kw: object) -> ModelAutomationLivenessDeadmanRequest:
    base: dict[str, object] = {
        "now": T0,
        "primary_host": "host-p",
        "canary_host": "host-p",
        "primary_last_heartbeat_at": T0 - timedelta(minutes=30),
    }
    base.update(kw)
    return ModelAutomationLivenessDeadmanRequest.model_validate(base)


def test_deadman_primary_silent_direct_with_the_bus_unreachable(
    tmp_path: Path,
) -> None:
    result = HandlerAutomationLivenessDeadman().handle(_request(broker_reachable=False))
    assert result.delivery_route is EnumDeadmanDeliveryRoute.DIRECT
    assert {a.verdict for a in result.alarms_raised} == {
        _V.PRIMARY_SILENT,
        _V.BUS_SILENT,
    }
    primary = next(a for a in result.alarms_raised if a.verdict is _V.PRIMARY_SILENT)
    assert primary.reason is EnumAutomationLivenessReason.PRIMARY_HEARTBEAT_MISSED
    assert primary.host == "host-p"
    # Silent since the heartbeat aged past its bound, not since this tick.
    assert primary.opened_at == T0 - timedelta(minutes=20)

    # The alarm reaches the alert channel without the bus: the sink refuses
    # every publish and the Slack message is still delivered with its timestamp.
    channel = FakeChannel()
    sink = RecordingSink()
    sink.refuse = True
    delivery = HandlerAutomationLivenessWatchdogEffect(
        store=OutboxStore(tmp_path / "watchdog.sqlite"),
        sender=channel,
        history=channel,
        escalation=FakeEscalation(),
        sink=sink,
    )
    out = delivery.handle(
        ModelWatchdogDeliveryRequest(now=T0, alarms_raised=result.alarms_raised)
    )
    assert [d.delivered for d in out.delivered] == [True, True]
    assert all(d.message_ref for d in out.delivered)
    assert out.publish_failures == 2
    assert len(channel.messages) == 2


def test_deadman_primary_silent_with_the_bus_up_names_no_bus_alarm() -> None:
    result = HandlerAutomationLivenessDeadman().handle(_request())
    assert result.delivery_route is EnumDeadmanDeliveryRoute.BUS
    assert [a.verdict for a in result.alarms_raised] == [_V.PRIMARY_SILENT]


def test_deadman_primary_never_heard_is_silent_from_now() -> None:
    result = HandlerAutomationLivenessDeadman().handle(
        _request(primary_last_heartbeat_at=None)
    )
    (alarm,) = result.alarms_raised
    assert alarm.verdict is _V.PRIMARY_SILENT
    assert alarm.opened_at == T0
    assert alarm.evidence_ref == "deadman:primary-heartbeat=never"


def test_deadman_fresh_primary_heartbeat_raises_nothing() -> None:
    """Positive control for the silent case above: same code path, no alarm."""
    result = HandlerAutomationLivenessDeadman().handle(
        _request(primary_last_heartbeat_at=T0 - timedelta(seconds=30))
    )
    assert result.alarms_raised == ()


def test_deadman_same_silence_names_the_same_episode_on_every_tick() -> None:
    compute = HandlerAutomationLivenessDeadman()
    first = compute.handle(_request())
    later = compute.handle(_request(now=T0 + timedelta(minutes=5)))
    assert first.alarms_raised[0].episode_id == later.alarms_raised[0].episode_id
    assert first.alarms_raised[0].opened_at == later.alarms_raised[0].opened_at


def test_deadman_a_new_heartbeat_then_a_new_silence_is_a_new_episode() -> None:
    compute = HandlerAutomationLivenessDeadman()
    first = compute.handle(_request())
    second = compute.handle(
        _request(
            now=T0 + timedelta(hours=2),
            primary_last_heartbeat_at=T0 + timedelta(hours=1),
        )
    )
    assert first.alarms_raised[0].episode_id != second.alarms_raised[0].episode_id
