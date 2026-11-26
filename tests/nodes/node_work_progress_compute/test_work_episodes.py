# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Episode judgment: one alert per crossing, one close per resumed thing, four watched kinds."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.lab_alarm_delivery import EnumDeliveryCondition
from omnimarket.nodes.node_work_progress_compute import NodeWorkEpisodesCompute
from omnimarket.nodes.node_work_progress_compute.models import (
    EnumEpisodeClosed,
    EnumWatchedKind,
    ModelWatchedThing,
    ModelWorkEpisodeRequest,
    ModelWorkEpisodeResult,
)
from omnimarket.nodes.node_work_progress_compute.overlay import (
    NODE_NAME,
    load_episode_budgets,
    load_episode_phases,
)
from tests.node_overlay_support import install_node_overlay

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
LIMITS = {
    "lane": {"placed": 600, "claimed": 1800, "pr_open": 3600},
    "pr": {
        "draft": 14400,
        "ci_pending": 2700,
        "red_unowned": 1800,
        "green_unarmed": 1800,
        "armed": 3600,
    },
    "round": {"awaiting_next_run": 7200},
    "tick": {"overdue": 1800, "running": 1500},
}


def _thing(
    kind: EnumWatchedKind,
    key: str,
    phase: str,
    spent: float,
    anchor: str = "row-1",
) -> ModelWatchedThing:
    return ModelWatchedThing(
        kind=kind,
        key=key,
        phase=phase,
        anchor_at=NOW - timedelta(seconds=spent),
        anchor_id=anchor,
        owner="orchestrator-31d9-swarm",
    )


def _judge(
    things: tuple[ModelWatchedThing, ...],
    *,
    now: datetime = NOW,
    carried: ModelWorkEpisodeResult | None = None,
    complete: tuple[EnumWatchedKind, ...] = (),
) -> ModelWorkEpisodeResult:
    return NodeWorkEpisodesCompute().handle(
        ModelWorkEpisodeRequest(
            correlation_id="corr-1",
            now=now,
            budgets=LIMITS,
            things=things,
            open_episodes=carried.open_episodes if carried else (),
            complete_kinds=complete,
        )
    )


def test_a_lane_past_its_phase_budget_alerts_once_per_crossing() -> None:
    lane = _thing(EnumWatchedKind.LANE, "stall-1615", "claimed", 1801)
    first = _judge((lane,))
    assert [c.crossing_id for c in first.crossings] == ["lane|stall-1615|claimed|row-1"]
    assert first.crossings[0].budget_seconds == 1800
    assert first.crossings[0].spent_seconds == 1801
    assert first.alarm_request is not None
    assert first.alarm_request.outcome is EnumDeliveryCondition.ALARM
    assert [a.subject for a in first.alarm_request.alarms] == [
        "lane|stall-1615|claimed|row-1"
    ]

    # Positive control: the same crossing judged on later ticks alerts nothing.
    carried = first
    for minutes in (5, 10, 60):
        again = _judge((lane,), now=NOW + timedelta(minutes=minutes), carried=carried)
        assert again.crossings == ()
        assert again.alarm_request is None
        assert len(again.still_open) == 1
        carried = again


def test_the_budget_boundary_is_strict() -> None:
    at = _judge((_thing(EnumWatchedKind.LANE, "a", "claimed", 1800),))
    over = _judge((_thing(EnumWatchedKind.LANE, "a", "claimed", 1801),))
    assert at.crossings == ()
    assert at.alarm_request is None
    assert len(over.crossings) == 1


def test_a_resumed_lane_closes_the_episode_and_a_new_stall_is_a_new_crossing() -> None:
    stalled = _thing(EnumWatchedKind.LANE, "stall-1615", "claimed", 2400, "row-1")
    first = _judge((stalled,))
    assert len(first.open_episodes) == 1

    # The lane resumed: a newer row moved its anchor and it is inside its budget.
    resumed = _thing(EnumWatchedKind.LANE, "stall-1615", "claimed", 60, "row-2")
    closed = _judge((resumed,), carried=first)
    assert [r.reason for r in closed.recoveries] == [EnumEpisodeClosed.MOVED]
    assert closed.crossings == ()
    assert closed.open_episodes == ()
    assert closed.alarm_request is not None
    assert closed.alarm_request.outcome is EnumDeliveryCondition.OK
    assert closed.alarm_request.alarms == ()

    # Stalls again on the new anchor: a second crossing, a second alert.
    later = _thing(EnumWatchedKind.LANE, "stall-1615", "claimed", 1900, "row-2")
    second = _judge((later,), now=NOW + timedelta(minutes=30), carried=closed)
    assert [c.crossing_id for c in second.crossings] == [
        "lane|stall-1615|claimed|row-2"
    ]


def test_a_lane_that_changes_phase_or_ends_closes_its_episode() -> None:
    first = _judge((_thing(EnumWatchedKind.LANE, "x", "claimed", 3000),))
    changed = _judge(
        (_thing(EnumWatchedKind.LANE, "x", "pr_open", 30, "row-9"),), carried=first
    )
    assert [r.reason for r in changed.recoveries] == [EnumEpisodeClosed.PHASE_CHANGED]
    assert changed.open_episodes == ()

    # Absent from a reading that enumerated every lane: it ended.
    gone = _judge((), carried=first, complete=(EnumWatchedKind.LANE,))
    assert [r.reason for r in gone.recoveries] == [EnumEpisodeClosed.GONE]
    # Absent from a reading that did not say it was complete: unknown, the episode stays open.
    unknown = _judge((), carried=first)
    assert unknown.recoveries == ()
    assert len(unknown.still_open) == 1
    assert unknown.alarm_request is None


@pytest.mark.parametrize(
    ("kind", "phase", "limit"),
    [
        (EnumWatchedKind.LANE, "pr_open", 3600),
        (EnumWatchedKind.PR, "ci_pending", 2700),
        (EnumWatchedKind.PR, "red_unowned", 1800),
        (EnumWatchedKind.TICK, "running", 1500),
        (EnumWatchedKind.TICK, "overdue", 1800),
    ],
)
def test_each_kind_alerts_once_past_its_own_phase_budget(
    kind: EnumWatchedKind, phase: str, limit: int
) -> None:
    under = _thing(kind, "thing", phase, limit)
    over = _thing(kind, "thing", phase, limit + 1)
    assert _judge((under,)).alarm_request is None
    first = _judge((over,))
    assert len(first.crossings) == 1
    assert first.crossings[0].budget_seconds == limit
    assert _judge((over,), carried=first).alarm_request is None


def test_two_things_cross_independently_and_the_alarm_names_each() -> None:
    first = _judge(
        (
            _thing(EnumWatchedKind.LANE, "a", "claimed", 2000),
            _thing(EnumWatchedKind.PR, "omnimarket#1", "ci_pending", 3000),
            _thing(EnumWatchedKind.LANE, "b", "claimed", 100),
        )
    )
    assert [c.key for c in first.crossings] == ["a", "omnimarket#1"]
    assert first.alarm_request is not None
    assert len(first.alarm_request.alarms) == 2
    # One resumes, one stays: no OK request while an episode remains open.
    next_ = _judge(
        (
            _thing(EnumWatchedKind.LANE, "a", "claimed", 5, "row-2"),
            _thing(EnumWatchedKind.PR, "omnimarket#1", "ci_pending", 3100),
        ),
        carried=first,
    )
    assert [r.key for r in next_.recoveries] == ["a"]
    assert [e.key for e in next_.still_open] == ["omnimarket#1"]
    assert next_.alarm_request is None


def test_a_thing_with_no_budget_for_its_phase_is_refused() -> None:
    with pytest.raises(ValueError, match="no budget for lane phase 'nope'"):
        _judge((_thing(EnumWatchedKind.LANE, "a", "nope", 5),))


def test_the_handler_reads_no_clock_or_environment() -> None:
    import inspect

    from omnimarket.nodes.node_work_progress_compute.handlers import (
        handler_work_episodes,
    )

    source = inspect.getsource(handler_work_episodes)
    for forbidden in ("datetime.now", "time.time", "os.environ", "open("):
        assert forbidden not in source


def test_overlay_limits_are_required_for_every_contract_phase(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_node_overlay(
        monkeypatch,
        tmp_path,
        NODE_NAME,
        {
            "budget_seconds": {"placed": 1, "claimed": 1, "pr_open": 1},
            "episode_limit_seconds": LIMITS,
        },
    )
    assert load_episode_budgets() == LIMITS
    assert set(load_episode_phases()) == set(LIMITS)

    broken = {k: dict(v) for k, v in LIMITS.items()}
    del broken["tick"]["running"]
    broken["pr"]["bogus"] = 5
    install_node_overlay(
        monkeypatch,
        tmp_path,
        NODE_NAME,
        {
            "budget_seconds": {"placed": 1, "claimed": 1, "pr_open": 1},
            "episode_limit_seconds": broken,
        },
    )
    with pytest.raises(NodeOverlayError) as raised:
        load_episode_budgets()
    assert f"missing {NODE_NAME}.episode_limit_seconds.tick.running" in str(
        raised.value
    )
    assert f"unknown {NODE_NAME}.episode_limit_seconds.pr.bogus" in str(raised.value)

    install_node_overlay(
        monkeypatch,
        tmp_path,
        NODE_NAME,
        {"budget_seconds": {"placed": 1, "claimed": 1, "pr_open": 1}},
    )
    with pytest.raises(NodeOverlayError, match=r"missing .*episode_limit_seconds"):
        load_episode_budgets()


def test_the_contract_declares_the_operation_topics_and_phases() -> None:
    contract = yaml.safe_load(
        (
            Path(__file__).resolve().parents[3]
            / "src/omnimarket/nodes/node_work_progress_compute/contract.yaml"
        ).read_text("utf-8")
    )
    operation = next(
        h
        for h in contract["handler_routing"]["handlers"]
        if h["operation"] == "judge_work_episodes"
    )
    assert operation["topic"] in contract["event_bus"]["subscribe_topics"]
    assert operation["topic"] == "onex.cmd.omnimarket.work-episodes-judge-requested.v1"
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]
    assert set(contract["config"]["work_episodes"]["kinds"]) == {
        "lane",
        "pr",
        "round",
        "tick",
    }
