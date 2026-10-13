# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC4: an observer going silent sends one HOST_SILENT, a host with a live
observer and many failing processes keeps per-process verdicts, and flapping
inside the hysteresis sends nothing (OMN-20806)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from omnimarket.models.liveness.model_automation_liveness import (
    EnumAutomationEmitter,
    EnumAutomationLivenessVerdict,
    ModelAutomationLivenessEntry,
)
from omnimarket.nodes.node_automation_liveness_watchdog_compute.models.model_automation_liveness_watchdog import (
    ModelBusFacts,
)
from tests.nodes.node_automation_liveness_watchdog_compute.support import (
    T0,
    Driver,
    entry,
    facts,
    observer,
    overlay,
    raised_for,
    run,
    verdict_of,
)

pytestmark = pytest.mark.unit

_V = EnumAutomationLivenessVerdict
_OBS = EnumAutomationEmitter.OBSERVER


def _fleet(host: str, count: int) -> list[ModelAutomationLivenessEntry]:
    return [entry(f"{host}/job-{i}", host=host, emitter=_OBS) for i in range(count)]


def test_watchdog_folding_and_hysteresis_silent_observer_sends_one() -> None:
    silent, live = _fleet("host-a", 12), _fleet("host-b", 12)
    driver = Driver(overlay(*silent, *live))
    stale_run = T0 - timedelta(hours=2)  # past every max_silence of 1200 s
    all_facts = tuple(facts(e, [run("old", stale_run)]) for e in (*silent, *live))

    for minute in range(4):
        now = T0 + timedelta(minutes=minute)
        result = driver.tick(
            now,
            processes=all_facts,
            observers=(
                observer("host-a", T0 - timedelta(minutes=30)),
                observer("host-b", now - timedelta(seconds=30)),
            ),
        )

    # One message for the dead host...
    host_a = [r for r in driver.raised if r.host == "host-a"]
    assert [(r.process_id, r.verdict) for r in host_a] == [
        ("host-a/observer", _V.HOST_SILENT)
    ]
    for e in silent:
        v = verdict_of(result, e.process_id)
        assert (v.verdict, v.folded_into, v.episode_id) == (
            _V.MISSED,
            _V.HOST_SILENT,
            None,
        )
    # ...and every failing process on the live host keeps its own episode.
    host_b = [r for r in driver.raised if r.host == "host-b"]
    assert sorted(r.process_id for r in host_b) == sorted(e.process_id for e in live)
    assert all(r.verdict is _V.MISSED for r in host_b)
    assert all(verdict_of(result, e.process_id).folded_into is None for e in live)


def test_watchdog_folding_and_hysteresis_count_of_missed_never_folds() -> None:
    fleet = _fleet("host-a", 30)
    driver = Driver(overlay(*fleet))
    stale = tuple(facts(e, [run("old", T0 - timedelta(hours=2))]) for e in fleet)
    for minute in range(3):
        now = T0 + timedelta(minutes=minute)
        driver.tick(
            now,
            processes=stale,
            observers=(observer("host-a", now - timedelta(seconds=10)),),
        )
    assert len(driver.raised) == 30
    assert not raised_for(driver.raised, "host-a/observer")


def test_watchdog_folding_and_hysteresis_flapping_sends_nothing() -> None:
    job = entry("host-a/flappy")
    driver = Driver(overlay(job))
    for minute in range(12):
        now = T0 + timedelta(minutes=minute)
        # Every other evaluation the newest run is past max_silence.
        last = now - timedelta(seconds=1500 if minute % 2 == 0 else 60)
        driver.tick(now, processes=(facts(job, [run(f"r{minute}", last)]),))
    assert driver.raised == []
    assert driver.cleared == []


def test_watchdog_folding_and_hysteresis_open_episode_survives_flapping() -> None:
    job = entry("host-a/flappy")
    driver = Driver(overlay(job))
    stale = facts(job, [run("old", T0 - timedelta(hours=1))])
    driver.tick(T0, processes=(stale,))
    driver.tick(T0 + timedelta(minutes=1), processes=(stale,))
    [opened] = driver.raised
    for minute in range(2, 10):
        now = T0 + timedelta(minutes=minute)
        fresh = facts(job, [run(f"r{minute}", now - timedelta(seconds=30))])
        driver.tick(now, processes=(fresh if minute % 2 else stale,))
    # One healthy evaluation at a time never closes it, and nothing re-sends.
    assert driver.raised == [opened]
    assert driver.cleared == []
    # Two healthy evaluations in a row do.
    for minute in (10, 11):
        now = T0 + timedelta(minutes=minute)
        driver.tick(now, processes=(facts(job, [run("ok", now)]),))
    assert [c.episode_id for c in driver.cleared] == [opened.episode_id]


def test_watchdog_folding_and_hysteresis_silent_bus_folds_everything() -> None:
    fleet = _fleet("host-a", 5)
    driver = Driver(overlay(*fleet))
    stale = tuple(facts(e, [run("old", T0 - timedelta(hours=2))]) for e in fleet)
    bus = ModelBusFacts(
        bus_process_id="bus/broker",
        host="host-d",
        last_event_at=T0 - timedelta(minutes=30),
        broker_answered=False,
    )
    for minute in range(3):
        now = T0 + timedelta(minutes=minute)
        result = driver.tick(
            now,
            processes=stale,
            observers=(observer("host-a", T0 - timedelta(minutes=30)),),
            bus=bus,
        )
    assert [(r.process_id, r.verdict) for r in driver.raised] == [
        ("bus/broker", _V.BUS_SILENT)
    ]
    assert verdict_of(result, "host-a/observer").folded_into is _V.BUS_SILENT
