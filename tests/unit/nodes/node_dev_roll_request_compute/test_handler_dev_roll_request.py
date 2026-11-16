# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Table tests of the batching decision, every rule in the order the handler checks them."""

from __future__ import annotations

import random
from datetime import timedelta
from pathlib import Path

import pytest

from omnimarket.handlers.node_overlay_reader import NodeOverlayNotFoundError
from omnimarket.models.dev_roll import (
    EnumDevRollAction,
    EnumDevRollEventRefusal,
    EnumDevRollHealth,
    EnumDevRollHoldReason,
    ModelDevRollDecided,
    ModelDevRollRecord,
)
from omnimarket.nodes.node_dev_roll_request_compute.handlers import (
    HandlerDevRollRequest,
    decide_dev_roll,
)
from omnimarket.nodes.node_dev_roll_request_compute.models import (
    EnumDevRollRefusalReason,
    ModelDevRollPolicy,
    ModelDevRollRefused,
)
from omnimarket.nodes.node_dev_roll_request_compute.overlay import load_dev_roll_policy
from tests.unit.nodes.node_dev_roll_request_compute.conftest import (
    NOW,
    facts,
    roll,
    tip,
    write_overlay,
)

pytestmark = pytest.mark.unit


def _decided(result: object) -> ModelDevRollDecided:
    assert isinstance(result, ModelDevRollDecided)
    return result


def test_no_pending_events_holds(policy: ModelDevRollPolicy) -> None:
    d = _decided(decide_dev_roll(facts(()), policy))
    assert d.action is EnumDevRollAction.HOLD
    assert d.hold_reason is EnumDevRollHoldReason.NO_PENDING_EVENTS
    assert d.would_request is None
    assert d.shadow is True


def test_five_tip_events_inside_one_window_wait_then_make_one_roll(
    policy: ModelDevRollPolicy,
) -> None:
    events = tuple(tip(age_seconds=60 * n) for n in (20, 15, 10, 5, 1))
    held = _decided(decide_dev_roll(facts(events), policy))
    assert held.hold_reason is EnumDevRollHoldReason.WINDOW_OPEN
    assert held.hold_until == events[0].merged_at + timedelta(seconds=1800)
    assert held.longest_wait_seconds == 20 * 60
    aged = tuple(tip(age_seconds=1800 + 60 * n) for n in (4, 3, 2, 1, 0))
    rolled = _decided(decide_dev_roll(facts(aged), policy))
    assert rolled.action is EnumDevRollAction.ROLL_NOW
    assert len(rolled.rolled_event_ids) == 5
    assert rolled.would_request is not None
    assert rolled.longest_wait_seconds == 1800 + 240


def test_the_roll_is_at_the_newest_build_context_sha(
    policy: ModelDevRollPolicy,
) -> None:
    older = tip("omnibase_infra", age_seconds=4000, sha="1" * 40)
    newer = tip("omnibase_infra", age_seconds=2500, sha="2" * 40)
    other = tip("omnimarket", age_seconds=2000, sha="3" * 40)
    d = _decided(decide_dev_roll(facts((older, other, newer)), policy))
    assert d.would_request is not None
    assert d.would_request.git_ref == "2" * 40
    assert d.newest_sha_by_repository == {
        "omnibase_infra": "2" * 40,
        "omnimarket": "3" * 40,
    }
    assert d.would_request.requested_by == "test-requester"
    assert d.would_request.requested_at == NOW


def test_a_batch_without_a_build_context_event_rolls_the_fallback_ref(
    policy: ModelDevRollPolicy,
) -> None:
    d = _decided(decide_dev_roll(facts((tip(age_seconds=3000),)), policy))
    assert d.would_request is not None
    assert d.would_request.git_ref == "origin/dev"


def test_a_roll_still_running_holds_without_a_time(policy: ModelDevRollPolicy) -> None:
    last = roll(
        started_ago=300,
        finished_ago=None,
        health=EnumDevRollHealth.UNKNOWN,
        healthy_ago=None,
    )
    d = _decided(decide_dev_roll(facts((tip(age_seconds=3000),), last), policy))
    assert d.hold_reason is EnumDevRollHoldReason.ROLL_IN_PROGRESS
    assert d.hold_until is None


def test_a_request_inside_the_minimum_start_interval_waits(
    policy: ModelDevRollPolicy,
) -> None:
    last = roll(started_ago=1000, finished_ago=100, healthy_ago=100)
    d = _decided(decide_dev_roll(facts((tip(age_seconds=3000),), last), policy))
    assert d.hold_reason is EnumDevRollHoldReason.MIN_START_INTERVAL
    assert d.hold_until == NOW - timedelta(seconds=1000) + timedelta(seconds=1800)


def test_a_previous_roll_not_yet_healthy_for_the_bound_holds_until_it_is(
    policy: ModelDevRollPolicy,
) -> None:
    last = roll(started_ago=2500, finished_ago=200, healthy_ago=200)
    d = _decided(decide_dev_roll(facts((tip(age_seconds=3000),), last), policy))
    assert d.hold_reason is EnumDevRollHoldReason.PREVIOUS_ROLL_NOT_HEALTHY
    assert d.hold_until == NOW - timedelta(seconds=200) + timedelta(seconds=600)


@pytest.mark.parametrize(
    "health", [EnumDevRollHealth.UNHEALTHY, EnumDevRollHealth.UNKNOWN]
)
def test_a_failed_roll_inside_the_release_bound_holds_until_the_bound(
    policy: ModelDevRollPolicy, health: EnumDevRollHealth
) -> None:
    last = roll(started_ago=5000, finished_ago=1000, health=health, healthy_ago=None)
    d = _decided(decide_dev_roll(facts((tip(age_seconds=3000),), last), policy))
    assert d.hold_reason is EnumDevRollHoldReason.PREVIOUS_ROLL_NOT_HEALTHY
    assert d.hold_until == NOW - timedelta(seconds=1000) + timedelta(seconds=3600)
    assert d.released_failed_roll_job_id is None


@pytest.mark.parametrize(
    "health", [EnumDevRollHealth.UNHEALTHY, EnumDevRollHealth.UNKNOWN]
)
def test_a_failed_roll_past_the_release_bound_releases_the_hold(
    policy: ModelDevRollPolicy, health: EnumDevRollHealth
) -> None:
    last = roll(started_ago=5000, finished_ago=3700, health=health, healthy_ago=None)
    d = _decided(decide_dev_roll(facts((tip(age_seconds=3000),), last), policy))
    assert d.action is EnumDevRollAction.ROLL_NOW
    assert d.released_failed_roll_job_id == "job-1"


def test_a_healthy_roll_releases_no_failed_roll(
    policy: ModelDevRollPolicy,
) -> None:
    last = roll(started_ago=5000, finished_ago=3700, healthy_ago=3700)
    d = _decided(decide_dev_roll(facts((tip(age_seconds=3000),), last), policy))
    assert d.action is EnumDevRollAction.ROLL_NOW
    assert d.released_failed_roll_job_id is None


def test_a_roll_healthy_for_the_bound_and_spaced_rolls_now(
    policy: ModelDevRollPolicy,
) -> None:
    last = roll(started_ago=4000, finished_ago=3000, healthy_ago=3000)
    d = _decided(decide_dev_roll(facts((tip(age_seconds=3000),), last), policy))
    assert d.action is EnumDevRollAction.ROLL_NOW


def test_an_unknown_repository_is_refused_on_the_decision_not_dropped(
    policy: ModelDevRollPolicy,
) -> None:
    bad = tip("somewhere_else", age_seconds=5000)
    good = tip(age_seconds=5000)
    d = _decided(decide_dev_roll(facts((bad, good)), policy))
    assert d.action is EnumDevRollAction.ROLL_NOW
    assert d.rolled_event_ids == (good.event_id,)
    (refused,) = d.refused_events
    assert refused.event_id == bad.event_id
    assert refused.reason is EnumDevRollEventRefusal.UNKNOWN_REPOSITORY


def test_only_unknown_events_hold_with_no_pending_events_and_the_refusal(
    policy: ModelDevRollPolicy,
) -> None:
    bad = tip("somewhere_else", age_seconds=5000)
    d = _decided(decide_dev_roll(facts((bad,)), policy))
    assert d.hold_reason is EnumDevRollHoldReason.NO_PENDING_EVENTS
    assert len(d.refused_events) == 1


def test_an_event_from_the_future_is_refused(policy: ModelDevRollPolicy) -> None:
    future = tip(age_seconds=-30)
    d = _decided(decide_dev_roll(facts((future,)), policy))
    assert d.refused_events[0].reason is EnumDevRollEventRefusal.EVENT_AFTER_OBSERVED_AT


def test_input_order_does_not_change_the_decision(policy: ModelDevRollPolicy) -> None:
    events = [tip(age_seconds=2000 + 37 * n, sha=f"{n:040x}") for n in range(8)]
    base = facts(tuple(events))
    expected = decide_dev_roll(base, policy)
    rng = random.Random(7)
    for _ in range(5):
        rng.shuffle(events)
        assert (
            decide_dev_roll(
                base.model_copy(update={"pending_events": tuple(events)}), policy
            )
            == expected
        )


def test_facts_that_contradict_themselves_are_refused_as_a_whole(
    policy: ModelDevRollPolicy,
) -> None:
    future = ModelDevRollRecord(
        job_id="j",
        git_ref="c" * 40,
        runtime_started_at=NOW - timedelta(seconds=100),
        finished_at=NOW + timedelta(seconds=100),
        health=EnumDevRollHealth.HEALTHY,
        healthy_since=NOW + timedelta(seconds=100),
    )
    result = decide_dev_roll(facts((tip(age_seconds=3000),), future), policy)
    assert isinstance(result, ModelDevRollRefused)
    assert result.reason is EnumDevRollRefusalReason.INCONSISTENT_LAST_ROLL


async def test_the_handler_class_answers_with_the_same_decision(
    policy: ModelDevRollPolicy,
) -> None:
    f = facts((tip(age_seconds=3000),))
    assert await HandlerDevRollRequest(policy).handle(f) == decide_dev_roll(f, policy)


def test_a_missing_overlay_refuses_with_no_packaged_fallback(tmp_path: Path) -> None:
    with pytest.raises(NodeOverlayNotFoundError):
        load_dev_roll_policy({"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})


def test_an_overlay_value_changes_the_decision(tmp_path: Path) -> None:
    doc = {
        "batch_window_seconds": 60,
        "min_start_interval_seconds": 60,
        "healthy_for_seconds": 60,
    }
    from tests.unit.nodes.node_dev_roll_request_compute.conftest import POLICY_DOC

    short = load_dev_roll_policy(write_overlay(tmp_path, {**POLICY_DOC, **doc}))
    d = _decided(decide_dev_roll(facts((tip(age_seconds=120),)), short))
    assert d.action is EnumDevRollAction.ROLL_NOW


def test_an_overlay_missing_a_value_refuses(tmp_path: Path) -> None:
    from tests.unit.nodes.node_dev_roll_request_compute.conftest import POLICY_DOC

    partial = {k: v for k, v in POLICY_DOC.items() if k != "healthy_for_seconds"}
    with pytest.raises(ValueError, match="healthy_for_seconds"):
        load_dev_roll_policy(write_overlay(tmp_path, partial))


def test_failing_control_the_per_change_rule_rolls_five_times_where_the_node_rolls_once(
    policy: ModelDevRollPolicy,
) -> None:
    """Today's rule is one roll request per merged change; the node's window makes one."""
    events = tuple(tip(age_seconds=1800 + 10 * n) for n in range(5))

    def per_change_rule(pending: tuple[object, ...]) -> list[object]:
        return [object() for _ in pending]

    assert len(per_change_rule(events)) == 5
    d = _decided(decide_dev_roll(facts(events), policy))
    assert d.action is EnumDevRollAction.ROLL_NOW
    assert d.would_request is not None
    assert len(d.rolled_event_ids) == 5, "one request covers all five events"


async def test_the_runtime_built_handler_reads_its_policy_from_the_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: ModelDevRollPolicy
) -> None:
    """The runtime constructs the handler with no arguments (infra auto-wiring)."""
    for key, value in write_overlay(tmp_path).items():
        monkeypatch.setenv(key, value)
    f = facts((tip(age_seconds=3000),))
    assert await HandlerDevRollRequest().handle(f) == decide_dev_roll(f, policy)


async def test_the_runtime_built_handler_refuses_with_no_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    with pytest.raises(NodeOverlayNotFoundError):
        await HandlerDevRollRequest().handle(facts((tip(age_seconds=3000),)))
