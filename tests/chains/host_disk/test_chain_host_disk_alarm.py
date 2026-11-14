# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC5 (OMN-23866): a host under its floor, a stale reading and a missing reading each reach the alert channel.

The alarm is a slack-publish command on the bus, addressed to the overlay's alarm channel. Every test runs with the
process environment cleared of anything the alarm could read, so the alarm never depends on a host's env file. The
control drops the slack-publish topic on the wire and the same assertion goes red. The dev broker half runs when
HOST_DISK_CHAIN_LANE and HOST_DISK_CHAIN_ROOT name a declared bus lane.
"""

from __future__ import annotations

import datetime as dt

import pytest

from omnimarket.nodes.node_slack_publish_effect.models.model_slack_publish import (
    ModelSlackPublish,
)
from tests.nodes.node_host_disk_decision_compute.decision_inputs import GIB
from tests.nodes.node_host_disk_orchestrator.orchestrator_inputs import (
    HOSTS,
    SLACK_TOPIC,
    Orchestrator,
    policy,
)

from .host_disk_chain import (
    DevBrokerWire,
    DroppingWire,
    InMemoryWire,
    Wire,
    dev_broker_target,
    new_key,
    run_chain,
)

pytestmark = pytest.mark.unit

FLOOR_ABOVE_FREE = (
    900 * GIB
)  # the test overlay: every host's floor sits above its 500 GiB free


@pytest.fixture(autouse=True)
def _no_host_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    for name in list(os.environ):
        if name.startswith(("SLACK", "ONEX_", "HOST_DISK")) and name not in (
            "HOST_DISK_CHAIN_LANE",
            "HOST_DISK_CHAIN_ROOT",
        ):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", "/nonexistent-host-disk-alarm-test")


async def _alarms(wire: Wire, key: str, expected: int) -> list[ModelSlackPublish]:
    return [
        ModelSlackPublish.model_validate(raw)
        for raw in await wire.read(SLACK_TOPIC, key, expected)
    ]


async def _under_floor(wire: Wire) -> None:
    key = new_key()
    orch = Orchestrator(pol=policy(floor_free_bytes=FLOOR_ABOVE_FREE))
    await run_chain(wire, orch, key, stale_revisions=0)
    alarms = await _alarms(wire, key, len(HOSTS))
    assert len(alarms) == len(HOSTS)
    assert {a.channel for a in alarms} == {orch.overlay.alarm_channel}
    for host in HOSTS:
        assert any(
            host in (a.text or "") and "under_floor" in (a.text or "") for a in alarms
        )


async def _stale_and_missing(wire: Wire) -> None:
    key = new_key()
    orch = Orchestrator()
    # host a read three hours ago (stale); host b never read (missing)
    await run_chain(
        wire,
        orch,
        key,
        read_hosts=(HOSTS[0],),
        reading_age=dt.timedelta(hours=3),
        stale_revisions=0,
    )
    alarms = await _alarms(wire, key, 2)
    texts = sorted(a.text or "" for a in alarms)
    assert len(texts) == 2
    assert any("missing_reading" in t and HOSTS[1] in t for t in texts)
    assert any("stale_reading" in t and HOSTS[0] in t for t in texts)


async def test_a_host_under_its_floor_reaches_the_alert_channel_in_memory() -> None:
    await _under_floor(InMemoryWire())


async def test_a_stale_and_a_missing_reading_alarm_in_memory() -> None:
    await _stale_and_missing(InMemoryWire())


async def test_a_host_over_its_floor_raises_no_alarm_in_memory() -> None:
    wire = InMemoryWire()
    key = new_key()
    await run_chain(wire, Orchestrator(), key, stale_revisions=0)
    assert await _alarms(wire, key, 0) == []


async def test_the_alarm_goes_red_when_the_alarm_command_is_dropped() -> None:
    wire = DroppingWire(frozenset({SLACK_TOPIC}))
    with pytest.raises(AssertionError):
        await _under_floor(wire)


@pytest.mark.kafka
@pytest.mark.integration
async def test_a_host_under_its_floor_reaches_the_alert_channel_on_the_real_dev_broker() -> (
    None
):
    lane, root = dev_broker_target()
    async with DevBrokerWire(lane, root, new_key()) as wire:
        await _under_floor(wire)


@pytest.mark.kafka
@pytest.mark.integration
async def test_a_stale_and_a_missing_reading_alarm_on_the_real_dev_broker() -> None:
    lane, root = dev_broker_target()
    async with DevBrokerWire(lane, root, new_key()) as wire:
        await _stale_and_missing(wire)
