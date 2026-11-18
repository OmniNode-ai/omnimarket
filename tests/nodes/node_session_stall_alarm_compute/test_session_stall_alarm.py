# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The session stall judge replays the recorded 2026-11-17 queue rows (OMN-25405, criterion 1).

The judge reads the rows it is given and ``now``; no clock and no file. The fixture marks each row quoted or shaped.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.models.session_stall import (
    EnumQueueOperation,
    EnumSessionStallReason,
    ModelQueueRow,
    ModelSessionStallRequest,
    ModelSessionStallVerdict,
)
from omnimarket.nodes.node_session_stall_alarm_compute.handlers.handler_session_stall_judge import (
    HandlerSessionStallJudge,
)

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).parent / "fixtures" / "queue_rows_2026-11-17.yaml"


def _at(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def _rows() -> tuple[ModelQueueRow, ...]:
    document = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    return tuple(
        ModelQueueRow(at=_at(row["at"]), operation=EnumQueueOperation(row["operation"]))
        for row in document["rows"]
    )


def _judge(
    now: datetime, rows: tuple[ModelQueueRow, ...], *, tick_declared: bool = True
) -> ModelSessionStallVerdict:
    return HandlerSessionStallJudge().handle(
        ModelSessionStallRequest(now=now, rows=rows, tick_declared=tick_declared)
    )


def _replay(
    start: str, end: str, rows: tuple[ModelQueueRow, ...]
) -> list[tuple[datetime, ModelSessionStallVerdict]]:
    now, stop = _at(start), _at(end)
    out: list[tuple[datetime, ModelSessionStallVerdict]] = []
    while now <= stop:
        # Only the rows the transcript held at that minute are visible to the judge.
        out.append((now, _judge(now, tuple(r for r in rows if r.at <= now))))
        now += timedelta(minutes=1)
    return out


def _episodes(verdicts: list[tuple[datetime, ModelSessionStallVerdict]]) -> int:
    """Count the runs of consecutive stalled minutes: one run is one stall."""
    count, inside = 0, False
    for _, verdict in verdicts:
        if verdict.stalled and not inside:
            count += 1
        inside = verdict.stalled
    return count


def test_session_stall_alarm_replay_raises_exactly_one_stall_by_0814z() -> None:
    verdicts = _replay("2026-11-17T07:52:00Z", "2026-11-17T11:38:00Z", _rows())
    assert _episodes(verdicts) == 1
    first = next(at for at, verdict in verdicts if verdict.stalled)
    assert first == _at("2026-11-17T08:14:00Z")
    # The minute before, the oldest enqueue (07:58:57Z) has waited 14 min 03 s: not yet.
    before = dict(verdicts)[_at("2026-11-17T08:13:00Z")]
    assert not before.stalled


def test_session_stall_alarm_replay_names_both_reasons_and_clears_at_the_drain() -> (
    None
):
    verdicts = dict(_replay("2026-11-17T07:52:00Z", "2026-11-17T11:38:00Z", _rows()))
    early = verdicts[_at("2026-11-17T08:14:00Z")]
    assert early.reasons == (EnumSessionStallReason.OLDEST_ENQUEUE_UNDEQUEUED,)
    assert early.oldest_enqueue_at == _at("2026-11-17T07:58:57Z")
    assert early.queued == 2
    # The 45-minute bar on scheduled tasks (07:47:56Z + 45 min = 08:32:56Z) joins the first reason.
    late = verdicts[_at("2026-11-17T08:33:00Z")]
    assert late.reasons == (
        EnumSessionStallReason.OLDEST_ENQUEUE_UNDEQUEUED,
        EnumSessionStallReason.NO_SCHEDULED_TASK,
    )
    assert late.last_task_at == _at("2026-11-17T07:47:56Z")
    assert verdicts[_at("2026-11-17T11:36:00Z")].stalled
    assert not verdicts[_at("2026-11-17T11:38:00Z")].stalled


def test_session_stall_alarm_none_on_the_0500z_to_0750z_slice() -> None:
    verdicts = _replay("2026-11-17T05:00:00Z", "2026-11-17T07:50:00Z", _rows())
    assert len(verdicts) == 171
    assert [at for at, verdict in verdicts if verdict.stalled] == []


def test_session_stall_alarm_positive_control_the_slice_stalls_when_the_ticks_stop() -> (
    None
):
    # Control for the zero above: cut the same slice's rows after 06:47:56Z and the judge must raise by 07:33:00Z.
    cut = tuple(r for r in _rows() if r.at <= _at("2026-11-17T06:47:56Z"))
    verdicts = _replay("2026-11-17T05:00:00Z", "2026-11-17T07:50:00Z", cut)
    first = next(at for at, verdict in verdicts if verdict.stalled)
    assert first == _at("2026-11-17T07:33:00Z")
    assert verdicts[-1][1].reasons == (EnumSessionStallReason.NO_SCHEDULED_TASK,)


def test_session_stall_alarm_the_tick_bar_applies_only_when_a_tick_is_declared() -> (
    None
):
    rows = tuple(r for r in _rows() if r.at <= _at("2026-11-17T07:47:56Z"))
    assert _judge(_at("2026-11-17T09:00:00Z"), rows).stalled
    assert not _judge(_at("2026-11-17T09:00:00Z"), rows, tick_declared=False).stalled


def test_session_stall_alarm_a_dequeue_or_remove_ends_the_oldest_wait() -> None:
    enqueue = ModelQueueRow(
        at=_at("2026-11-17T10:00:00Z"), operation=EnumQueueOperation.ENQUEUE
    )
    removed = ModelQueueRow(
        at=_at("2026-11-17T10:05:00Z"), operation=EnumQueueOperation.REMOVE
    )
    now = _at("2026-11-17T10:30:00Z")
    assert _judge(now, (enqueue,), tick_declared=False).stalled
    assert not _judge(now, (enqueue, removed), tick_declared=False).stalled


def test_session_stall_alarm_no_rows_is_not_a_stall() -> None:
    verdict = _judge(_at("2026-11-17T10:30:00Z"), ())
    assert not verdict.stalled
    assert verdict.reasons == ()


def test_session_stall_alarm_the_judge_is_the_same_on_unordered_rows() -> None:
    rows = _rows()
    now = _at("2026-11-17T09:00:00Z")
    visible = tuple(r for r in rows if r.at <= now)
    assert _judge(now, tuple(reversed(visible))) == _judge(now, visible)
