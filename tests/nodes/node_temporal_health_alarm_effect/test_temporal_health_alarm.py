# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Temporal health alarm: one alarm row per episode, one recovery row, the 10-minute poll rule at its boundary."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.nodes.node_temporal_health_alarm_effect.handlers.handler_temporal_health_alarm import (
    HandlerTemporalHealthAlarm,
    evaluate,
)
from omnimarket.nodes.node_temporal_health_alarm_effect.handlers.temporal_cli_reader import (
    read_facts,
)
from omnimarket.nodes.node_temporal_health_alarm_effect.models.model_temporal_health_alarm import (
    ModelTemporalHealthAlarmRequest,
    ModelTemporalHealthAlarmResult,
    ModelTemporalHealthFacts,
    ModelTemporalHealthSettings,
)
from omnimarket.nodes.node_temporal_health_alarm_effect.runtime_temporal_health_alarm import (
    load_settings,
    main,
)

NOW = datetime(2026, 11, 16, 16, 0, 0, tzinfo=UTC)
SETTINGS = ModelTemporalHealthSettings(
    row_ticket="OMN-24900", alarm_owner="orchestrator"
)


class Recorder:
    def __init__(self, error: str = "") -> None:
        self.rows: list[str] = []
        self.error = error

    def __call__(self, row: str) -> str:
        if self.error:
            return self.error
        self.rows.append(row)
        return ""


def _facts(
    *,
    server: bool = True,
    worker: bool = True,
    poll_age_s: float | None = 5.0,
    at: datetime = NOW,
) -> ModelTemporalHealthFacts:
    newest = None if poll_age_s is None else at - timedelta(seconds=poll_age_s)
    return ModelTemporalHealthFacts(
        read_at=at, server_answers=server, worker_polling=worker, newest_poll_at=newest
    )


def _tick(
    handler: HandlerTemporalHealthAlarm, facts: ModelTemporalHealthFacts, state: Path
) -> ModelTemporalHealthAlarmResult:
    return handler.handle(
        ModelTemporalHealthAlarmRequest(facts=facts, host="h051", state_dir=state)
    )


def test_stopped_worker_writes_one_alarm_row(tmp_path: Path) -> None:
    """Fails if the alarm branch is removed: an unhealthy reading must append an alarm row."""
    rec = Recorder()
    handler = HandlerTemporalHealthAlarm(settings=SETTINGS, append=rec)
    result = _tick(handler, _facts(worker=False, poll_age_s=None), tmp_path)
    assert result.verdict == "unhealthy"
    assert result.row_kind == "alarm"
    assert result.outcome == "appended"
    assert len(rec.rows) == 1
    row = rec.rows[0]
    assert " | STATUS | " in row
    for cell in (
        "src=alarm",
        "alarm=temporal-down",
        "state=alarm",
        "host=h051",
        "ticket=OMN-24900",
    ):
        assert f"| {cell} |" in row
    assert "reasons=worker-not-polling,no-task-polled" in row


def test_server_down_alarms(tmp_path: Path) -> None:
    rec = Recorder()
    handler = HandlerTemporalHealthAlarm(settings=SETTINGS, append=rec)
    _tick(handler, _facts(server=False, worker=False, poll_age_s=None), tmp_path)
    assert len(rec.rows) == 1
    assert "reasons=server-down,worker-not-polling,no-task-polled" in rec.rows[0]


def test_alarm_once_per_episode_then_recovery(tmp_path: Path) -> None:
    rec = Recorder()
    handler = HandlerTemporalHealthAlarm(settings=SETTINGS, append=rec)
    for minute in range(5):
        _tick(
            handler,
            _facts(worker=False, poll_age_s=None, at=NOW + timedelta(minutes=minute)),
            tmp_path,
        )
    assert len(rec.rows) == 1, rec.rows
    recovered = _tick(handler, _facts(at=NOW + timedelta(minutes=6)), tmp_path)
    assert recovered.row_kind == "recovery"
    assert len(rec.rows) == 2
    assert "| state=recovered |" in rec.rows[1]
    episode = "episode=2026-11-16T16:00:00Z"
    assert episode in rec.rows[0]
    assert episode in rec.rows[1]
    _tick(handler, _facts(at=NOW + timedelta(minutes=7)), tmp_path)
    assert len(rec.rows) == 2
    # a second outage is a new episode with its own alarm row
    _tick(
        handler,
        _facts(worker=False, poll_age_s=None, at=NOW + timedelta(minutes=8)),
        tmp_path,
    )
    assert len(rec.rows) == 3
    assert "episode=2026-11-16T16:08:00Z" in rec.rows[2]


def test_healthy_writes_nothing(tmp_path: Path) -> None:
    rec = Recorder()
    handler = HandlerTemporalHealthAlarm(settings=SETTINGS, append=rec)
    result = _tick(handler, _facts(), tmp_path)
    assert result.verdict == "healthy"
    assert rec.rows == []


@pytest.mark.parametrize(
    ("age_s", "stale"),
    [(0.0, False), (599.0, False), (599.999, False), (600.0, True), (601.0, True)],
)
def test_ten_minute_rule_boundary(age_s: float, stale: bool) -> None:
    reasons = evaluate(_facts(poll_age_s=age_s), SETTINGS)
    assert ("no-task-polled" in reasons) is stale


def test_stale_poll_alarms_with_server_and_worker_up(tmp_path: Path) -> None:
    rec = Recorder()
    handler = HandlerTemporalHealthAlarm(settings=SETTINGS, append=rec)
    _tick(handler, _facts(poll_age_s=600.0), tmp_path)
    assert len(rec.rows) == 1
    assert "reasons=no-task-polled" in rec.rows[0]


def test_unappended_alarm_is_retried_next_tick(tmp_path: Path) -> None:
    failing = Recorder(error="bus down")
    handler = HandlerTemporalHealthAlarm(settings=SETTINGS, append=failing)
    result = _tick(handler, _facts(worker=False, poll_age_s=None), tmp_path)
    assert result.outcome == "unappended"
    assert not (tmp_path / "episode.json").exists()
    rec = Recorder()
    handler = HandlerTemporalHealthAlarm(settings=SETTINGS, append=rec)
    _tick(handler, _facts(worker=False, poll_age_s=None), tmp_path)
    assert len(rec.rows) == 1


def _runner(
    health: tuple[int, str, str], describe: tuple[int, str, str]
) -> Callable[[list[str], float], tuple[int, str, str]]:
    def run(argv: list[str], timeout_s: float) -> tuple[int, str, str]:
        return health if "health" in argv else describe

    return run


def test_reader_parses_pollers() -> None:
    doc = {
        "pollers": [
            {"identity": "w1", "lastAccessTime": "2026-11-16T15:59:50Z"},
            {"identity": "w2", "lastAccessTime": "2026-11-16T15:58:00.5Z"},
        ]
    }
    facts = read_facts(
        SETTINGS,
        now=NOW,
        runner=_runner((0, "SERVING\n", ""), (0, json.dumps(doc), "")),
    )
    assert facts.server_answers
    assert facts.worker_polling
    assert facts.newest_poll_at == datetime(2026, 11, 16, 15, 59, 50, tzinfo=UTC)


def test_reader_no_pollers_and_missing_cli() -> None:
    facts = read_facts(
        SETTINGS, now=NOW, runner=_runner((0, "SERVING", ""), (0, "{}", ""))
    )
    assert facts.server_answers
    assert not facts.worker_polling
    assert facts.newest_poll_at is None
    down = read_facts(
        SETTINGS, now=NOW, runner=_runner((127, "", "not found: temporal"), (0, "", ""))
    )
    assert not down.server_answers
    assert not down.worker_polling
    assert "exit=127" in down.detail


def test_contract_settings_and_probe(capsys: pytest.CaptureFixture[str]) -> None:
    settings = load_settings({"ONEX_TEMPORAL_TASK_QUEUE": "q1"})
    assert settings.poll_stale_after_s == 600
    assert settings.task_queue == "q1"
    assert main(["--probe"]) == 0
    assert json.loads(capsys.readouterr().out)["probe"] == "ok"
