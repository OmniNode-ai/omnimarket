# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The MVP launch block (OMN-23205, plan task M1): the 17 ids, the overlay loader, the newest-run rule at its
boundaries over recorded histories, the candidate check and the composite readings. Every case asserts state and reason."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.models.floor_17a.model_mvp_launch import (
    MVP_READING_IDS,
    ModelCron,
    ModelMvpCandidate,
    ModelMvpLaunchBlock,
    ModelMvpLaunchOverlay,
    ModelMvpReading,
    ModelMvpSourceHistory,
)
from omnimarket.nodes.node_product_floor_verdict_compute.handlers.handler_mvp_launch import (
    HandlerMvpLaunch,
)
from tests.nodes.node_floor_simple_effect.mvp_support import (
    CANDIDATE,
    MVP_NOW,
    WALK,
    all_legs,
    green_world,
    mac_leg_name,
    overlay_dict,
    request,
    run,
    with_history,
)

pytestmark = pytest.mark.unit


def block(req: Any = None) -> ModelMvpLaunchBlock:
    return HandlerMvpLaunch().handle(req or request())


def reading(b: ModelMvpLaunchBlock, item_id: str) -> ModelMvpReading:
    return next(r for r in (*b.readings, *b.scenarios) if r.id == item_id)


def at(h: int, m: int = 0, s: int = 20, day: int = 8) -> datetime:
    return datetime(2026, 11, day, h, m, s, tzinfo=UTC)


# --- AC1: the block has exactly the 17 ids ---------------------------------------------------------------------------


def test_golden_all_green_world_reads_working_with_exactly_the_17_ids() -> None:
    b = block()
    assert sorted(r.id for r in b.readings) == sorted(MVP_READING_IDS)
    assert len(b.readings) == 17
    assert b.state == "WORKING"
    assert all(r.state == "WORKING" and r.reason == "ok" for r in b.readings)
    assert [s.id for s in b.scenarios] == [f"R{n}" for n in range(1, 10)]
    c11 = reading(b, "C11")
    assert c11.latest_due_slot == datetime(2026, 11, 8, 6, 0, tzinfo=UTC)
    assert c11.newest_run_conclusion == "success"
    assert c11.last_success == at(12)
    assert c11.age_s == 1780
    assert c11.graded_versions == CANDIDATE


# --- AC2: the loader refuses an incomplete overlay --------------------------------------------------------------------


def _edit(fn: Any) -> dict[str, Any]:
    raw = copy.deepcopy(overlay_dict())
    fn(raw)
    return raw


def _spec(raw: dict[str, Any], item_id: str, key: str = "readings") -> dict[str, Any]:
    return next(s for s in raw[key] if s["id"] == item_id)


def test_overlay_loads_when_complete() -> None:
    assert ModelMvpLaunchOverlay.model_validate(overlay_dict())


def test_overlay_missing_grace_refused() -> None:
    raw = _edit(lambda r: _spec(r, "C11").pop("grace_s"))
    with pytest.raises(ValidationError, match=r"C11.*grace"):
        ModelMvpLaunchOverlay.model_validate(raw)


def test_overlay_missing_kind_refused() -> None:
    raw = _edit(lambda r: _spec(r, "C12").pop("kind"))
    with pytest.raises(ValidationError, match="kind"):
        ModelMvpLaunchOverlay.model_validate(raw)
    dropped = _edit(lambda r: r["readings"].remove(_spec(r, "C12")))
    with pytest.raises(ValidationError, match="C12"):
        ModelMvpLaunchOverlay.model_validate(dropped)


def test_overlay_missing_scenario_refused() -> None:
    raw = _edit(lambda r: r["scenarios"].remove(_spec(r, "R7", "scenarios")))
    with pytest.raises(ValidationError, match="R7"):
        ModelMvpLaunchOverlay.model_validate(raw)


def test_overlay_noncron_schedule_refused() -> None:
    raw = _edit(lambda r: _spec(r, WALK).update(cron="weekly"))
    with pytest.raises(ValidationError, match="five field cron"):
        ModelMvpLaunchOverlay.model_validate(raw)


def test_derived_with_cron_refused() -> None:
    raw = _edit(lambda r: _spec(r, "release-identity").update(cron="0 9 * * 1"))
    with pytest.raises(ValidationError, match="derived reading has no cron"):
        ModelMvpLaunchOverlay.model_validate(raw)


@pytest.mark.parametrize("item_id", ["C13", "C14", "C29", "C31", "C32"])
def test_overlay_customer_machine_without_mac_leg_refused(item_id: str) -> None:
    raw = _edit(lambda r: _spec(r, item_id).pop("mac_leg"))
    with pytest.raises(ValidationError, match="Mac leg"):
        ModelMvpLaunchOverlay.model_validate(raw)


@pytest.mark.parametrize("item_id", ["R1", "R2", "R3", "R5"])
def test_overlay_customer_machine_scenario_without_mac_leg_refused(
    item_id: str,
) -> None:
    raw = _edit(lambda r: _spec(r, item_id, "scenarios").pop("mac_leg"))
    with pytest.raises(ValidationError, match="Mac leg"):
        ModelMvpLaunchOverlay.model_validate(raw)


def test_overlay_c33_must_name_all_nine_scenarios() -> None:
    raw = _edit(lambda r: _spec(r, "C33").update(inputs=["R1", "R2"]))
    with pytest.raises(ValidationError, match="nine scenario"):
        ModelMvpLaunchOverlay.model_validate(raw)


# --- the cron rule ----------------------------------------------------------------------------------------------------


def test_cron_latest_at_or_before_is_inclusive_and_floors_seconds() -> None:
    cron = ModelCron.parse("0 */6 * * *")
    assert cron.latest_at_or_before(at(12, 0, 0)) == at(12, 0, 0)
    assert cron.latest_at_or_before(at(11, 59, 59)) == at(6, 0, 0)
    weekly = ModelCron.parse("0 9 * * 1")
    assert weekly.latest_at_or_before(MVP_NOW) == datetime(
        2026, 11, 2, 9, 0, tzinfo=UTC
    )


# --- AC3: the newest-run rule at its boundaries -----------------------------------------------------------------------


def test_latest_slot_failed_is_not_working_with_the_conclusion_as_reason() -> None:
    older_ok = run("a", at(0, 0))
    newest_bad = run("b", at(6, 0), conclusion="failure")
    b = block(request(with_history("C11", [older_ok, newest_bad])))
    r = reading(b, "C11")
    assert (r.state, r.reason) == ("NOT WORKING", "failure")
    assert r.last_success == at(0, 0)
    assert b.state == "NOT WORKING"
    assert "C11: failure" in b.named


def test_unreadable_is_not_working() -> None:
    world = with_history("C12", [], error="run-list refused")
    r = reading(block(request(world)), "C12")
    assert (r.state, r.reason, r.detail) == (
        "NOT WORKING",
        "unreadable",
        "run-list refused",
    )
    world = green_world()
    del world["C12"]
    r = reading(block(request(world)), "C12")
    assert (r.state, r.reason) == ("NOT WORKING", "unreadable")


def test_latest_running_is_not_working() -> None:
    running = run("b", at(6, 0), conclusion=None, status="in_progress")
    r = reading(block(request(with_history("C11", [running]))), "C11")
    assert (r.state, r.reason) == ("NOT WORKING", "latest-running")


def test_older_success_not_used_while_latest_running() -> None:
    ok = run("a", at(0, 0))
    running = run("b", at(6, 0, 30), conclusion=None, status="queued")
    r = reading(block(request(with_history("C11", [ok, running]))), "C11")
    assert (r.state, r.reason) == ("NOT WORKING", "latest-running")
    assert r.last_success == at(0, 0)


def test_uncreated_slot_inside_grace_newest_run_decides() -> None:
    # The 12:00 slot is inside its one hour grace at 12:30 and has no run; the 06:00 run decides.
    ok = run("a", at(6, 0, 30))
    r = reading(block(request(with_history("C11", [ok]))), "C11")
    assert (r.state, r.reason) == ("WORKING", "ok")
    assert r.latest_due_slot == datetime(2026, 11, 8, 6, 0, tzinfo=UTC)
    bad = run("b", at(6, 0, 30), conclusion="failure")
    r = reading(block(request(with_history("C11", [bad]))), "C11")
    assert (r.state, r.reason) == ("NOT WORKING", "failure")


def test_missed_slot_is_not_working() -> None:
    # Grace over (13:05), slot 12:00 has no run, only the 06:00 run exists.
    now = datetime(2026, 11, 8, 13, 5, tzinfo=UTC)
    ok = run("a", at(6, 0, 30))
    r = reading(block(request(with_history("C11", [ok]), now=now)), "C11")
    assert (r.state, r.reason) == ("NOT WORKING", "missed-slot")
    assert r.latest_due_slot == at(12, 0, 0)
    none = reading(block(request(with_history("C11", []))), "C11")
    assert (none.state, none.reason) == ("NOT WORKING", "missed-slot")


def test_missed_slot_ignores_runs_of_other_triggers() -> None:
    manual = run("m", at(6, 0, 30), event="workflow_dispatch")
    r = reading(block(request(with_history("C11", [manual]))), "C11")
    assert (r.state, r.reason) == ("NOT WORKING", "missed-slot")


def test_cron_boundary_exact() -> None:
    ok = run("a", at(6, 0, 0))
    # now minus grace equals the 12:00 slot exactly: the slot is due, so the 06:00 run is a missed slot.
    due_now = datetime(2026, 11, 8, 13, 0, tzinfo=UTC)
    r = reading(block(request(with_history("C11", [ok]), now=due_now)), "C11")
    assert (r.state, r.reason) == ("NOT WORKING", "missed-slot")
    assert r.latest_due_slot == at(12, 0, 0)
    # one second earlier the slot is still inside its grace.
    early = due_now - timedelta(seconds=1)
    r = reading(block(request(with_history("C11", [ok]), now=early)), "C11")
    assert (r.state, r.reason) == ("WORKING", "ok")
    assert r.latest_due_slot == at(6, 0, 0)
    # a run created at the slot instant counts as created at the slot.
    at_slot = run("b", at(12, 0, 0))
    r = reading(block(request(with_history("C11", [ok, at_slot]), now=due_now)), "C11")
    assert (r.state, r.reason) == ("WORKING", "ok")


@pytest.mark.parametrize("conclusion", ["cancelled", "skipped", "timed_out"])
def test_cancelled_is_not_working(conclusion: str) -> None:
    r = reading(
        block(
            request(
                with_history("C11", [run("a", at(6, 0, 30), conclusion=conclusion)])
            )
        ),
        "C11",
    )
    assert (r.state, r.reason) == ("NOT WORKING", conclusion)


def test_unreadable_conclusion_is_not_working() -> None:
    r = reading(
        block(request(with_history("C11", [run("a", at(6, 0, 30), conclusion=None)]))),
        "C11",
    )
    assert (r.state, r.reason) == ("NOT WORKING", "unreadable-conclusion")


def test_derived_input_unreadable() -> None:
    world = with_history("R4", [], error="scenario history refused")
    b = block(request(world))
    c33 = reading(b, "C33")
    assert (c33.state, c33.reason) == ("NOT WORKING", "input-unreadable:R4")
    assert b.state == "NOT WORKING"


def test_derived_input_not_working_names_the_input() -> None:
    world = with_history("R6", [run("a", at(6, 0, 30), conclusion="failure")])
    c33 = reading(block(request(world)), "C33")
    assert (c33.state, c33.reason, c33.detail) == (
        "NOT WORKING",
        "input-not-working:R6",
        "failure",
    )


def test_derived_release_identity_needs_a_readable_candidate() -> None:
    b = block(request(candidate=ModelMvpCandidate(error="manifest absent")))
    r = reading(b, "release-identity")
    assert (r.state, r.reason, r.detail) == (
        "NOT WORKING",
        "input-unreadable:candidate-manifest",
        "manifest absent",
    )


def test_derived_is_recomputed_from_the_inputs_it_is_given() -> None:
    b = block(request(with_history("onboarding-verify-native", [])))
    assert (
        reading(b, "release-identity").reason
        == "input-not-working:onboarding-verify-native"
    )


# --- AC4: graded versions against the candidate manifest ---------------------------------------------------------------


def test_not_the_candidate() -> None:
    other = run("a", at(6, 0, 30), versions={"onex": "1.2.2"})
    r = reading(block(request(with_history("C11", [other]))), "C11")
    assert (r.state, r.reason) == ("NOT WORKING", "not-the-candidate")


def test_not_the_candidate_when_the_run_graded_no_version() -> None:
    blank = run("a", at(6, 0, 30), versions={})
    r = reading(block(request(with_history("C11", [blank]))), "C11")
    assert (r.state, r.reason) == ("NOT WORKING", "not-the-candidate")


def test_not_the_candidate_when_the_manifest_is_unreadable() -> None:
    r = reading(block(request(candidate=ModelMvpCandidate(error="gone"))), "C11")
    assert (r.state, r.reason) == ("NOT WORKING", "candidate-unreadable")


# --- AC5: composite readings --------------------------------------------------------------------------------------------


def _walk(
    legs: dict[str, str] | None, *, created: datetime | None = None, **more: Any
) -> dict[str, ModelMvpSourceHistory]:
    return with_history(
        WALK,
        [
            run(
                "w",
                created or datetime(2026, 11, 2, 9, 0, 20, tzinfo=UTC),
                legs=legs,
                **more,
            )
        ],
    )


def test_composite_both_green() -> None:
    b = block(request())
    for item in ("C13", "C14", "C29", "C31", "C32", "R1", "R2", "R3", "R5"):
        r = reading(b, item)
        assert (r.state, r.reason, r.mac_leg_state) == ("WORKING", "ok", "WORKING"), (
            item
        )


def test_composite_linux_green_mac_leg_missing() -> None:
    legs = all_legs()
    del legs[mac_leg_name("C13")]
    b = block(request(_walk(legs)))
    r = reading(b, "C13")
    assert (r.state, r.reason) == ("NOT WORKING", "mac-leg-missing")
    assert reading(b, "C14").state == "WORKING"
    no_walk = block(request(with_history(WALK, [])))
    assert reading(no_walk, "C14").reason == "mac-leg-missing"


def test_composite_linux_green_mac_leg_failed() -> None:
    legs = all_legs()
    legs[mac_leg_name("C14")] = "NOT WORKING"
    b = block(request(_walk(legs)))
    r = reading(b, "C14")
    assert (r.state, r.reason, r.mac_leg_state) == (
        "NOT WORKING",
        "mac-leg-failed",
        "NOT WORKING",
    )
    assert reading(b, "C13").state == "WORKING"
    # a failed leg on one reading and on no other: R2 is untouched.
    assert reading(b, "R2").state == "WORKING"


def test_composite_mac_leg_stale() -> None:
    old = MVP_NOW - timedelta(days=7, seconds=1)
    b = block(request(_walk(all_legs(), created=old)))
    # the walk reading itself is a missed weekly slot, and every composite reading names the stale leg.
    assert reading(b, "C29").reason == "mac-leg-stale"
    edge = MVP_NOW - timedelta(days=7)
    b = block(request(_walk(all_legs(), created=edge)))
    assert reading(b, "C29").state == "WORKING"


def test_composite_mac_green_linux_failed() -> None:
    b = block(
        request(with_history("C29", [run("a", at(6, 0, 30), conclusion="failure")]))
    )
    r = reading(b, "C29")
    assert (r.state, r.reason, r.mac_leg_state) == ("NOT WORKING", "failure", None)


def test_composite_walk_on_another_version_is_not_the_candidate() -> None:
    b = block(request(_walk(all_legs(), versions={"onex": "0.0.1"})))
    assert reading(b, "C13").reason == "not-the-candidate"
    assert reading(b, WALK).reason == "not-the-candidate"


def test_composite_running_walk_has_no_legs_yet() -> None:
    b = block(request(_walk(all_legs(), conclusion=None, status="in_progress")))
    assert reading(b, "C13").reason == "mac-leg-missing"
    assert reading(b, WALK).reason == "latest-running"


def test_candidate_triggered_walk_counts_as_scheduled() -> None:
    cand = run("c", at(10, 0, 0, day=8), event="candidate", legs=all_legs())
    sched = run(
        "s", datetime(2026, 11, 2, 9, 0, 20, tzinfo=UTC), legs=all_legs("NOT WORKING")
    )
    b = block(request(with_history(WALK, [sched, cand])))
    assert reading(b, WALK).newest_run_id == "c"
    assert reading(b, "C13").state == "WORKING"
    # a candidate trigger on a reading that does not admit it does not count.
    other = run("x", at(6, 0, 30), event="candidate")
    assert (
        reading(block(request(with_history("C11", [other]))), "C11").reason
        == "missed-slot"
    )
