# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Headroom from the bus fails loudly when it is unknown, and fan-out is read back (OMN-20867).

AC-M2: a host the runner gives idle slots but no fresh headroom reading (none on the
bus, or older than the contract-declared max age) is a typed HEADROOM_UNKNOWN failure
for that host, never zero candidates and never success; a fresh reading with free
slots places on that host; and a tick's fan-out is counted only from dispatch records
read back from the bus for the same tick.
"""

from __future__ import annotations

import datetime as dt
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_infra.runtime.contract_terminal_events import (
    apply_failure_terminal_guard,
    declared_failure_terminal_topics,
    resolve_terminal_verdict,
)

from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)
from omnimarket.nodes.node_lab_fill_plan_compute.handlers import (
    HandlerLabFillFanoutReadback,
    HandlerLabFillHeadroomPlan,
)
from omnimarket.nodes.node_lab_fill_plan_compute.models import (
    EnumLabFillPlanFailure,
    ModelLabFillDispatchRecord,
    ModelLabFillFanoutRequest,
    ModelLabFillHeadroomPlanRequest,
    ModelLabFillHostProbe,
)

NOW = dt.datetime(2026, 10, 10, 8, 0, 0, tzinfo=dt.UTC)
GIB = 1024**3


def _contract() -> dict[str, Any]:
    contract: dict[str, Any] = yaml.safe_load(
        files("omnimarket.nodes.node_lab_fill_plan_compute")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    return contract


def _max_age() -> int:
    return int(_contract()["config"]["lab_fill_plan"]["headroom"]["max_age_seconds"])


def _probe(name: str, slots: int = 3, **extra: object) -> ModelLabFillHostProbe:
    return ModelLabFillHostProbe(
        name=name, runner_slots=slots, login_claude=True, **extra
    )


def _reading(name: str, age_s: float) -> ModelHostCapacityAdvertisement:
    return ModelHostCapacityAdvertisement(
        host_name=name,
        cores=32,
        load1=2.0,
        mem_available_bytes=100 * GIB,
        advertised_at=NOW - dt.timedelta(seconds=age_s),
        cadence_seconds=10,
    )


def _plan(
    probes: tuple[ModelLabFillHostProbe, ...],
    readings: tuple[ModelHostCapacityAdvertisement, ...],
) -> Any:
    return HandlerLabFillHeadroomPlan().handle(
        ModelLabFillHeadroomPlanRequest(
            tick_id="lab-fill-20261010T080000Z",
            observed_at=NOW,
            probes=probes,
            readings=readings,
        )
    )


def test_contract_declares_max_age_reading_topic_and_failure_terminal() -> None:
    contract = _contract()
    headroom = contract["config"]["lab_fill_plan"]["headroom"]
    assert headroom["reading_topic"] == (
        "onex.evt.omnimarket.lab-host-capacity-advertised.v1"
    )
    assert 0 < headroom["max_age_seconds"] <= 300
    terminals = contract["runtime_dispatch"]["terminal_events"]
    assert terminals["success"] == contract["terminal_event"]
    assert terminals["failure"] == "onex.evt.omnimarket.lab-fill-plan-failed.v1"
    assert terminals["failure"] in contract["event_bus"]["publish_topics"]
    assert terminals["failure"] in contract["externally_consumed_topics"]
    operations = {e["operation"] for e in contract["handler_routing"]["handlers"]}
    assert {"plan_lab_fill_headroom", "verify_lab_fill_fanout"} <= operations


def test_idle_slots_with_no_reading_is_a_typed_headroom_unknown_failure() -> None:
    result = _plan((_probe("host_a"),), ())
    assert result.terminal_failure_cause is EnumLabFillPlanFailure.HEADROOM_UNKNOWN
    assert [(u.host, u.reason) for u in result.unknown] == [("host_a", "missing")]
    assert result.unknown[0].failure is EnumLabFillPlanFailure.HEADROOM_UNKNOWN
    assert result.unknown[0].runner_slots == 3
    # Loud, not quiet: no lanes are invented and the verdict is a failure.
    assert (result.free, result.budget) == (0, 0)
    assert resolve_terminal_verdict(result) is False


def test_idle_slots_with_a_stale_reading_is_headroom_unknown() -> None:
    stale = _max_age() + 1
    result = _plan((_probe("host_a"),), (_reading("host_a", stale),))
    assert result.terminal_failure_cause is EnumLabFillPlanFailure.HEADROOM_UNKNOWN
    (unknown,) = result.unknown
    assert (unknown.host, unknown.reason) == ("host_a", "stale")
    assert unknown.reading_age_seconds == pytest.approx(stale)
    assert unknown.max_age_seconds == _max_age()


def test_a_reading_dated_beyond_max_age_in_the_future_is_not_fresh() -> None:
    result = _plan((_probe("host_a"),), (_reading("host_a", -(_max_age() + 1)),))
    assert [(u.host, u.reason) for u in result.unknown] == [("host_a", "future")]


def test_fresh_reading_with_free_slots_places_on_that_host() -> None:
    result = _plan((_probe("host_a"),), (_reading("host_a", 5),))
    assert result.terminal_failure_cause is None
    assert result.unknown == ()
    (host,) = result.hosts
    assert (host.name, host.lanes, host.reason) == ("host_a", 3, "")
    assert (result.free, result.budget) == (3, 3)
    assert resolve_terminal_verdict(result) is None


def test_the_newest_reading_of_a_host_decides() -> None:
    result = _plan(
        (_probe("host_a"),),
        (_reading("host_a", _max_age() + 30), _reading("host_a", 4)),
    )
    assert result.unknown == ()
    assert result.hosts[0].lanes == 3


def test_mixed_hosts_place_the_fresh_one_and_fail_loudly_for_the_unknown_one() -> None:
    result = _plan(
        (_probe("host_a"), _probe("host_b", slots=1)),
        (_reading("host_a", 5),),
    )
    by_name = {h.name: h for h in result.hosts}
    assert by_name["host_a"].lanes == 3
    assert by_name["host_b"].lanes == 0
    assert by_name["host_b"].reason == "headroom-unknown: missing"
    assert [u.host for u in result.unknown] == ["host_b"]
    assert result.free == 3
    assert result.terminal_failure_cause is EnumLabFillPlanFailure.HEADROOM_UNKNOWN


def test_hosts_without_idle_slots_or_blocked_by_the_runner_are_not_unknown() -> None:
    result = _plan(
        (
            _probe("full", slots=0),
            _probe("limited", limited="2026-10-10T09:00Z"),
            _probe("refused", refusal="load bar"),
            _probe("unreadable", error="rc=255"),
            _probe("mac", local=True),
        ),
        (),
    )
    assert result.unknown == ()
    assert result.terminal_failure_cause is None
    reasons = {h.name: h.reason for h in result.hosts}
    assert reasons == {
        "full": "runner-cap",
        "limited": "limited until 2026-10-10T09:00Z",
        "refused": "runner-refused: load bar",
        "unreadable": "unreadable: rc=255",
    }
    assert result.free == 0


def test_failure_result_routes_to_the_declared_failure_terminal() -> None:
    """Derived the way the event-bus result applier derives it, from the contract."""
    contract = _contract()
    success = contract["terminal_event"]
    path = files("omnimarket.nodes.node_lab_fill_plan_compute").joinpath(
        "contract.yaml"
    )
    failure_topics = declared_failure_terminal_topics(
        Path(str(path)),
        success_topic=success,
        publishable_topics=contract["event_bus"]["publish_topics"],
    )
    assert failure_topics == ("onex.evt.omnimarket.lab-fill-plan-failed.v1",)
    failed = _plan((_probe("host_a"),), ())
    placed = _plan((_probe("host_a"),), (_reading("host_a", 5),))
    route = {
        "success_topic": success,
        "failure_terminal_topics": failure_topics,
    }
    assert apply_failure_terminal_guard(failed, success, **route) == failure_topics[0]
    assert apply_failure_terminal_guard(placed, success, **route) == success


def _record(tick: str, lane: str, host: str = "host_a") -> ModelLabFillDispatchRecord:
    return ModelLabFillDispatchRecord(tick_id=tick, lane=lane, host=host)


def test_fanout_is_counted_only_from_records_of_the_same_tick() -> None:
    tick = "lab-fill-20261010T080000Z"
    result = HandlerLabFillFanoutReadback().handle(
        ModelLabFillFanoutRequest(
            tick_id=tick,
            claimed_lanes=("lane-1", "lane-2"),
            records=(
                _record(tick, "lane-1"),
                _record(tick, "lane-2", host="host_b"),
                _record("lab-fill-20261010T074000Z", "lane-old"),
            ),
        )
    )
    assert result.fanned_out == 2
    assert {r.lane for r in result.confirmed} == {"lane-1", "lane-2"}
    assert result.unconfirmed == ()
    assert result.other_tick_records == 1
    assert result.terminal_failure_cause is None


def test_a_fanout_claim_with_no_matching_record_fails() -> None:
    tick = "lab-fill-20261010T080000Z"
    result = HandlerLabFillFanoutReadback().handle(
        ModelLabFillFanoutRequest(
            tick_id=tick,
            claimed_lanes=("lane-1", "lane-2"),
            records=(
                _record(tick, "lane-1"),
                _record("lab-fill-20261010T074000Z", "lane-2"),
            ),
        )
    )
    assert result.fanned_out == 1
    assert result.unconfirmed == ("lane-2",)
    assert result.terminal_failure_cause is EnumLabFillPlanFailure.FANOUT_UNPROVEN
    assert resolve_terminal_verdict(result) is False


def test_a_fanout_claim_with_no_records_at_all_fails() -> None:
    result = HandlerLabFillFanoutReadback().handle(
        ModelLabFillFanoutRequest(
            tick_id="lab-fill-20261010T080000Z",
            claimed_lanes=("lane-1",),
            records=(),
        )
    )
    assert result.fanned_out == 0
    assert result.terminal_failure_cause is EnumLabFillPlanFailure.FANOUT_UNPROVEN
