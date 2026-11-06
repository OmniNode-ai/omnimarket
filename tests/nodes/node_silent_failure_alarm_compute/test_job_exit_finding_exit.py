# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A job whose nonzero exit is its own finding is not a crashed job (OMN-19998).

The recorded case: the floor alarm job exits 1 when floors are under. The job-exit alarm read
every such run as ``failed-exit`` and opened an episode each time the floors dipped under again
after the cooldown, 25 in all, each dispatching a lane told to find why the job exits nonzero.
None of those lanes acted on the floors, and the floors stopped at 18:40Z. A declared finding
exit opens a ``finding-exit`` episode whose cells and alert name the finding, so the dispatched
lane acts on what the job reports instead of on the job.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from omnimarket.nodes.node_silent_failure_alarm_compute.handlers.handler_job_exit_alarm import (
    HandlerJobExitAlarm,
)
from omnimarket.nodes.node_silent_failure_alarm_compute.models import (
    ModelJobExitAlarmRequest,
    ModelJobExitAlarmResult,
    ModelJobExitReading,
    ModelJobExitStreak,
)

LABEL = "ai.omninode.floor-alarm"
NOW = datetime(2026, 11, 4, 18, 0, tzinfo=UTC)


def _tick(
    runs: int,
    last_exit: int,
    previous: tuple[ModelJobExitStreak, ...],
    now: datetime,
    finding_exits: dict[str, tuple[int, ...]] | None = None,
) -> ModelJobExitAlarmResult:
    return HandlerJobExitAlarm().handle(
        ModelJobExitAlarmRequest(
            now=now,
            readable=True,
            readings=(
                ModelJobExitReading(label=LABEL, runs=runs, last_exit=last_exit),
            ),
            previous=previous,
            threshold=1,
            push_after_minutes=30,
            finding_exits=finding_exits or {},
        )
    )


def _baseline() -> tuple[ModelJobExitStreak, ...]:
    return (
        ModelJobExitStreak(
            label=LABEL, runs=10, last_exit=0, last_run_at=NOW - timedelta(minutes=5)
        ),
    )


def test_a_declared_finding_exit_opens_a_finding_episode_not_a_failed_exit() -> None:
    result = _tick(11, 1, _baseline(), NOW, {LABEL: (1,)})
    assert result.state == "FAILING"
    assert len(result.opened) == 1
    episode = result.opened[0]
    assert episode.cause == "finding-exit"
    assert episode.episode.endswith("-floor-alarm-finding")
    cell = result.friction_cells[0]
    assert "cause=finding-exit" in cell
    assert "reported its finding" in cell
    assert "not a crash" in cell


def test_the_finding_episode_push_names_the_finding() -> None:
    opened = _tick(11, 1, _baseline(), NOW, {LABEL: (1,)})
    later = _tick(12, 1, opened.streaks, NOW + timedelta(minutes=31), {LABEL: (1,)})
    assert len(later.push) == 1
    assert later.push[0].cause == "finding-exit"
    assert "reported its finding (exit 1)" in later.alert_text[0]


def test_an_undeclared_exit_of_the_same_job_stays_a_failed_exit() -> None:
    result = _tick(11, 2, _baseline(), NOW, {LABEL: (1,)})
    assert result.opened[0].cause == "failed-exit"


def test_without_a_declaration_exit_one_is_a_failed_exit_as_before() -> None:
    result = _tick(11, 1, _baseline(), NOW)
    assert result.opened[0].cause == "failed-exit"
    assert not result.opened[0].episode.endswith("-finding")


def test_the_same_snapshot_decides_the_same_episode() -> None:
    first = _tick(11, 1, _baseline(), NOW, {LABEL: (1,)})
    second = _tick(11, 1, _baseline(), NOW, {LABEL: (1,)})
    assert first == second


def test_the_finding_brief_asks_the_lane_to_act_on_the_finding(tmp_path: Path) -> None:
    from omnimarket.models.silent_failure_alarm import ModelJobExitEpisode
    from omnimarket.nodes.node_silent_failure_alarm_effect.handlers.handler_silent_failure_alarm import (
        HandlerSilentFailureAlarm,
    )
    from omnimarket.nodes.node_silent_failure_alarm_effect.models import (
        ModelSilentFailureAlarmOptions,
    )

    options = ModelSilentFailureAlarmOptions(
        registry_root=tmp_path,
        brief_dir=tmp_path / "briefs",
        job_finding_exits={LABEL: (1,)},
    )
    handler = HandlerSilentFailureAlarm(options)
    episode = ModelJobExitEpisode(
        episode="ep-20261104T1800Z-floor-alarm-finding",
        label=LABEL,
        lane_kind="job-exit-fix",
        streak=1,
        last_exit=1,
        cause="finding-exit",
    )
    dispatched, detail = handler._dispatch_job(episode)
    assert not dispatched
    assert "dispatch unconfigured" in detail
    brief = (tmp_path / "briefs" / f"{episode.episode}-brief.md").read_text()
    assert "is its declared finding, not a crash" in brief
    assert "never change the job so the finding exits 0" in brief
    assert "Find why" not in brief
