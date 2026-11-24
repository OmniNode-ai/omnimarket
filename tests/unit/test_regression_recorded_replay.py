# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC4 and AC5: the recorded probe results of 2026-11-23 replayed through the node.

The fixture holds the finished scheduled runs of the delegation checks as the SCM gateway returned them (run ids and finish
times). Replayed at 14:45Z, C14 and C29 are the only checks that went green to red, and the Qwen host change is named as
what changed between their last green and first red run.
"""

from __future__ import annotations

from datetime import UTC, datetime

from omnimarket.nodes.node_delegation_check_regression_effect.adapters.change_sources import (
    LedgerMarkerChangeSource,
)
from omnimarket.nodes.node_delegation_check_regression_effect.models.model_regression import (
    EnumChangeKind,
    ModelRegressionScanRequest,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from tests.unit.regression_support import TOPICS, Rig, recorded_runs, settings

REPLAY_AT = datetime(2026, 11, 23, 14, 45, tzinfo=UTC)
CHECKS = [
    {
        "check_id": c,
        "repository": "example-org/example-infra",
        "workflow": f"{c.lower()}.yml",
        "interval_seconds": 3600,
    }
    for c in ("C11", "C12", "C13", "C14", "C15", "C16", "C17", "C28", "C29", "C30")
]
MARKER = {"kind": "host_change", "row_type": "RULING", "contains_all": ["5090"]}
# A reduced ledger row: the ruling that took the model off the 5090 host at 13:46:25Z.
LEDGER = [
    "2026-11-23T13:46:25Z | RULING | lane=orchestrator | req=ruling-5090 | the model stays off the 5090 until told",
    "2026-11-23T13:50:00Z | RULING | lane=orchestrator | req=ruling-other | an unrelated ruling",
]


def _rig() -> Rig:
    # The replay window opens at 13:30Z: a transition older than 75 minutes is not part of it.
    cfg = settings(
        checks=CHECKS,
        change_markers=[MARKER],
        runs_per_check=40,
        max_transition_age_seconds=4500,
        flap_window_seconds=4500,
    )
    rig = Rig(recorded_runs(), cfg=cfg, until=REPLAY_AT)
    rig.handler._changes = LedgerMarkerChangeSource(
        InMemoryLedger(LEDGER), cfg.change_markers
    )  # type: ignore[attr-defined]
    return rig


def test_replay_at_1445z_raises_c14_and_c29_with_the_5090_host_change() -> None:
    rig = _rig()
    result = rig.handler.handle(ModelRegressionScanRequest(now=REPLAY_AT))
    by_check = {e.check_id: e for e in result.events}
    assert sorted(by_check) == ["C14", "C29"]
    assert by_check["C14"].last_green.run_id == "45185798731"
    assert by_check["C14"].first_red.run_id == "45196274344"
    assert by_check["C29"].last_green.run_id == "45179185106"
    assert by_check["C29"].first_red.run_id == "45189064181"
    for event in by_check.values():
        host = [c for c in event.changes if c.kind is EnumChangeKind.HOST_CHANGE]
        assert len(host) == 1, event.check_id
        assert "5090" in host[0].summary


def test_replay_latency_is_measured_from_the_red_run_to_the_replay_clock() -> None:
    result = _rig().handler.handle(ModelRegressionScanRequest(now=REPLAY_AT))
    by_check = {e.check_id: e for e in result.events}
    assert by_check["C14"].alarm_latency_seconds == 343.0
    assert by_check["C14"].late is False
    assert by_check["C29"].alarm_latency_seconds == 2665.0
    assert by_check["C29"].late is True


def test_replay_publishes_the_events_and_one_alarm_naming_both_checks() -> None:
    rig = _rig()
    rig.handler.handle(ModelRegressionScanRequest(now=REPLAY_AT))
    assert len(rig.publisher.on(TOPICS["regression_detected"])) == 2
    alarm_requests = [
        a
        for a in rig.publisher.on(TOPICS["alarm_delivery_requested"])
        if getattr(a, "alarms", ())
    ]
    assert len(alarm_requests) == 1
    assert sorted(a.subject.split("@")[0] for a in alarm_requests[0].alarms) == [
        "C14",
        "C29",
    ]  # type: ignore[attr-defined]


def test_replay_with_no_transition_detection_would_raise_nothing() -> None:
    """Positive control: the same fixture through a detector that finds no transition raises no event, so the test above fails on it."""
    import omnimarket.nodes.node_delegation_check_regression_effect.handlers.handler_delegation_check_regression as module

    original = module.find_transitions
    module.find_transitions = lambda _runs: []  # type: ignore[assignment]
    try:
        result = _rig().handler.handle(ModelRegressionScanRequest(now=REPLAY_AT))
    finally:
        module.find_transitions = original
    assert result.events == ()


def test_the_full_day_shows_c13_and_c11_transitions_the_board_did_not_alarm() -> None:
    # 13:00Z to midnight: the window that holds the afternoon and evening transitions.
    window = 11 * 3600
    rig = Rig(
        recorded_runs(),
        cfg=settings(
            checks=CHECKS,
            runs_per_check=100,
            max_transition_age_seconds=window,
            flap_window_seconds=window,
        ),
    )
    result = rig.handler.handle(
        ModelRegressionScanRequest(now=datetime(2026, 11, 24, 0, 0, tzinfo=UTC))
    )
    reds = {(e.check_id, e.first_red.at.strftime("%H:%M")) for e in result.events}
    assert ("C11", "20:14") in reds
    assert ("C13", "18:25") in reds
    assert ("C13", "22:23") in reds
    flaps = {f.check_id: f.count for f in result.flap_counts}
    assert flaps["C13"] == 1  # 18:25 red, 19:22 green: one flap, no flap alarm yet
