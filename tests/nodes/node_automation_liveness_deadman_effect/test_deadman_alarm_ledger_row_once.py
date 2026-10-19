# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Each opened and closed episode becomes one ledger row, once (OMN-20807, AC2)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from omnimarket.models.liveness.model_automation_liveness_deadman import (
    EnumDeadmanPhase,
    ModelAutomationLivenessDeadmanRequest,
)
from omnimarket.nodes.node_automation_liveness_deadman_compute.handlers.handler_automation_liveness_deadman import (
    HandlerAutomationLivenessDeadman,
)
from omnimarket.nodes.node_automation_liveness_deadman_effect.handlers.handler_automation_liveness_deadman_effect import (
    HandlerAutomationLivenessDeadmanEffect,
)
from omnimarket.nodes.node_automation_liveness_deadman_effect.handlers.recorded_store import (
    RecordedStore,
)
from omnimarket.nodes.node_automation_liveness_deadman_effect.models.model_deadman_record import (
    ModelDeadmanRecordRequest,
)
from tests.nodes.node_automation_liveness_deadman_effect.support import (
    T0,
    FakeLedger,
    SimulatedCrash,
    cleared,
    episode_id,
    key,
    make_effect,
    raised,
)

pytestmark = pytest.mark.unit

NOW = T0 + timedelta(hours=1)


def _rows(*, already: tuple = (), replay: bool = False):
    opened = raised("lab-fill", T0)
    closed = cleared("lab-fill", T0, T0 + timedelta(minutes=30))
    compute = HandlerAutomationLivenessDeadman().handle(
        ModelAutomationLivenessDeadmanRequest(
            now=NOW,
            primary_host="host-p",
            canary_host="host-p",
            primary_last_heartbeat_at=NOW,
            opened=(opened, opened) if replay else (opened,),
            closed=(closed,),
            already_recorded=already,
        )
    )
    return compute.ledger_rows


def _tick(handler: HandlerAutomationLivenessDeadmanEffect, rows, now=NOW):
    return handler.handle(
        ModelDeadmanRecordRequest(
            now=now, host="host-d", process_started_at=T0, rows=rows
        )
    )


def test_deadman_alarm_ledger_row_once_an_open_and_a_close_are_two_rows(
    tmp_path: Path,
) -> None:
    ledger = FakeLedger()
    out = _tick(make_effect(tmp_path, ledger), _rows())
    assert len(ledger.lines) == 2
    opened_row, closed_row = (line.split(" | ") for line in ledger.lines)
    assert opened_row[0] == "2026-10-12T08:00:00Z"
    assert closed_row[0] == "2026-10-12T08:30:00Z"
    for cells, phase in ((opened_row, "opened"), (closed_row, "closed")):
        assert cells[1] == "FRICTION"
        joined = " | ".join(cells)
        assert "| process=lab-fill " in joined
        assert "| verdict=missed " in joined
        assert f"| episode={episode_id('lab-fill')} " in joined
        assert f"| phase={phase} " in joined
        assert "| action=diagnose:lab-fill " in joined
        assert "existing=OMN-20769" in joined
    assert [r.ledger_line for r in out.recorded] == ["ledger:1", "ledger:2"]
    assert out.recorded[0].recorded_by == "watchdog/deadman"


def test_deadman_alarm_ledger_row_once_a_duplicate_delivery_writes_none(
    tmp_path: Path,
) -> None:
    ledger = FakeLedger()
    handler = make_effect(tmp_path, ledger)
    _tick(handler, _rows(replay=True))
    assert ledger.appends == 2  # the replayed open inside one request is one row

    again = _tick(handler, _rows(replay=True), NOW + timedelta(minutes=5))
    assert ledger.appends == 2
    assert len(again.skipped) == 2
    assert again.recorded == ()

    # The compute is told what the store holds and returns no rows at all.
    held = RecordedStore(tmp_path / "deadman.sqlite").recorded_keys()
    assert set(held) == {
        key("lab-fill", EnumDeadmanPhase.OPENED),
        key("lab-fill", EnumDeadmanPhase.CLOSED),
    }
    assert _rows(already=held) == ()


def test_deadman_alarm_ledger_row_once_a_crash_after_the_append_costs_no_duplicate(
    tmp_path: Path,
) -> None:
    ledger = FakeLedger()
    ledger.crash_after_append = True
    handler = make_effect(tmp_path, ledger)
    rows = _rows()
    with pytest.raises(SimulatedCrash):
        _tick(handler, rows)
    assert len(ledger.lines) == 1  # taken by the ledger, never reached the store

    restarted = make_effect(tmp_path, ledger)
    out = _tick(restarted, rows, NOW + timedelta(minutes=1))
    assert len(ledger.lines) == 2
    assert [r.episode_id for r in out.recorded] == [episode_id("lab-fill")] * 2
    assert out.recorded[0].ledger_line == "ledger:1"
    assert out.failure is None


def test_deadman_alarm_ledger_row_once_a_ledger_that_refuses_is_a_failure_not_a_skip(
    tmp_path: Path,
) -> None:
    ledger = FakeLedger()
    ledger.refuse_with = "ledger refused: no rule-4 cost sentence"
    out = _tick(make_effect(tmp_path, ledger), _rows())
    assert out.recorded == ()
    assert out.skipped == ()
    assert out.failure == "RuntimeError: ledger refused: no rule-4 cost sentence"
    assert len(out.backlog) == 2
    assert out.heartbeat.outbox_backlog == 2
