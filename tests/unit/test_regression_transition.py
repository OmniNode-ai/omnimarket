# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1: each green to red transition emits one regression event naming the check, both runs and what changed between them."""

from __future__ import annotations

from datetime import timedelta

from omnimarket.nodes.node_delegation_check_regression_effect.models.model_regression import (
    EnumChangeKind,
    ModelChange,
    ModelRegressionEvent,
    ModelRegressionScanRequest,
)
from tests.unit.regression_support import T0, TOPICS, Rig, runs


def _scan(rig: Rig, hours: float = 4.2) -> list[ModelRegressionEvent]:
    result = rig.handler.handle(
        ModelRegressionScanRequest(now=T0 + timedelta(hours=hours))
    )
    return list(result.events)


def test_green_to_red_emits_one_event_with_every_field() -> None:
    changes = [
        ModelChange(
            kind=EnumChangeKind.MERGE,
            at=T0 + timedelta(hours=2.1),
            ref="repo@abc",
            summary="a merge",
        ),
        ModelChange(
            kind=EnumChangeKind.DEV_BUILD,
            at=T0 + timedelta(hours=2.2),
            ref="def",
            summary="a dev build",
        ),
        ModelChange(
            kind=EnumChangeKind.HOST_CHANGE,
            at=T0 + timedelta(hours=2.3),
            summary="a host change",
        ),
        ModelChange(
            kind=EnumChangeKind.MODEL_SETTING,
            at=T0 + timedelta(hours=2.4),
            summary="a model setting",
        ),
        ModelChange(
            kind=EnumChangeKind.MERGE,
            at=T0 + timedelta(hours=3.5),
            summary="after the red run, not between",
        ),
    ]
    rig = Rig({"C14": runs("gggr"), "C13": runs("gggg")}, changes=changes)
    events = _scan(rig, hours=3.2)
    assert len(events) == 1
    event = events[0]
    assert event.check_id == "C14"
    assert event.last_green.run_id == "run-2"
    assert event.first_red.run_id == "run-3"
    assert [c.kind for c in event.changes] == [
        EnumChangeKind.MERGE,
        EnumChangeKind.DEV_BUILD,
        EnumChangeKind.HOST_CHANGE,
        EnumChangeKind.MODEL_SETTING,
    ]
    assert event.owner == "example-owner"
    assert event.alarm_lane == "example-orchestrator-lane"
    published = rig.publisher.on(TOPICS["regression_detected"])
    assert published == [event]


def test_no_event_without_a_green_before_the_red() -> None:
    rig = Rig({"C14": runs("rrrr"), "C13": runs("gggg")})
    assert _scan(rig) == []
    assert rig.publisher.on(TOPICS["regression_detected"]) == []


def test_two_transitions_of_one_check_are_two_events() -> None:
    rig = Rig({"C14": runs("grgr"), "C13": runs("gggg")})
    events = _scan(rig, hours=3.2)
    assert [e.first_red.run_id for e in events] == ["run-1", "run-3"]
    assert events[0].recovered_at is not None
    assert events[1].recovered_at is None


def test_a_scan_twice_raises_each_transition_once() -> None:
    rig = Rig({"C14": runs("gggr"), "C13": runs("gggg")})
    first = _scan(rig, hours=3.2)
    second = _scan(rig, hours=3.25)
    assert len(first) == 1
    assert second == []
    assert len(rig.publisher.on(TOPICS["regression_detected"])) == 1


def test_a_probe_scan_writes_nothing() -> None:
    rig = Rig({"C14": runs("gggr"), "C13": runs("gggg")})
    result = rig.handler.handle(
        ModelRegressionScanRequest(now=T0 + timedelta(hours=3.2), write=False)
    )
    assert len(result.events) == 1
    assert rig.publisher.published == []
    assert not rig.seen.seen(result.events[0].event_id)


def test_an_unreadable_check_is_named_and_never_counted() -> None:
    rig = Rig({"C13": runs("gggr")}, unreadable={"C14": "gateway timeout"})
    result = rig.handler.handle(
        ModelRegressionScanRequest(now=T0 + timedelta(hours=3.2))
    )
    assert [u.check_id for u in result.unread] == ["C14"]
    assert [e.check_id for e in result.events] == ["C13"]
