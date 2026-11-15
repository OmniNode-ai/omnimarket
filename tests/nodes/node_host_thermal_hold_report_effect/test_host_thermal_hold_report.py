# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The alarm decision and the outbox delivery of the host thermal hold report, over the in-memory ports."""

from __future__ import annotations

import uuid

import pytest

from omnimarket.nodes.node_host_thermal_hold_report_effect.handlers import (
    decide_alarm,
    row_with_req_last,
)
from omnimarket.nodes.node_host_thermal_hold_report_effect.handlers import (
    handler_host_thermal_hold_report as module,
)

from .conftest import ABSENT, LATCH, OUTBOX_ROW, outbox_row, present

pytestmark = pytest.mark.unit


def test_trip_posts_one_msg_naming_host_temperature_and_next_step(
    handler, guard, episodes, appender, request_for
) -> None:
    guard.latch = present(LATCH)
    result = handler.handle(request_for())
    assert result.alarm == "trip"
    assert result.alarm_status == "accepted"
    assert result.ok
    assert len(appender.sent) == 1
    request_id, row = appender.sent[0]
    assert request_id == result.alarm_request_id
    cells = row.split(" | ")
    assert cells[1] == "MSG"
    for wanted in (
        "from=example-thermal-hold-lab-a",
        "to=example-orchestrator",
        "state=tripped",
        "hottest=90.6C",
        "kind=thermal-hold",
        f"req={request_id}",
    ):
        assert wanted in cells
    assert cells[-2] == f"req={request_id}"
    assert "lab-a thermal guard" in cells[-1]
    assert "90.6 C" in cells[-1]
    assert "Operator next:" in cells[-1]
    assert episodes.record["open"] is True
    assert episodes.record["trip_key"] == LATCH["tripped_at"]


def _dedupe_holds(handler, guard, appender, request_for) -> None:
    guard.latch = present(LATCH)
    handler.handle(request_for())
    second = handler.handle(request_for())
    assert second.alarm == "none"
    assert len(appender.sent) == 1


def test_a_latch_that_stays_posts_no_second_alarm(
    handler, guard, appender, request_for
) -> None:
    _dedupe_holds(handler, guard, appender, request_for)


def test_failing_control_without_the_dedupe_a_second_alarm_is_sent(
    handler, guard, appender, request_for, monkeypatch
) -> None:
    monkeypatch.setattr(module, "decide_alarm", lambda *_: "trip")
    with pytest.raises(AssertionError):
        _dedupe_holds(handler, guard, appender, request_for)
    assert len(appender.sent) == 2


def test_release_posts_one_msg_with_the_last_reading_then_nothing(
    handler, guard, episodes, appender, request_for
) -> None:
    guard.latch = present(LATCH)
    handler.handle(request_for())
    guard.latch = ABSENT
    result = handler.handle(request_for())
    assert result.alarm == "release"
    assert result.ok
    cells = appender.sent[1][1].split(" | ")
    assert "state=released" in cells
    assert "hottest=72.9C" in cells
    assert episodes.record["open"] is False
    assert handler.handle(request_for()).alarm == "none"
    assert len(appender.sent) == 2


def test_a_pending_trip_is_retried_under_the_same_request_id(
    handler, guard, episodes, appender, request_for
) -> None:
    guard.latch = present(LATCH)
    appender.default = "pending"
    first = handler.handle(request_for())
    assert first.alarm_status == "pending"
    assert not first.ok
    assert episodes.record.get("open") is not True
    assert episodes.record["pending"] == {
        "kind": "trip",
        "request_id": first.alarm_request_id,
    }
    appender.default = "accepted"
    guard.latch = present(
        {**LATCH, "tripped_at": "2026-11-15T01:48:02Z"}
    )  # the guard rewrote its latch
    second = handler.handle(request_for())
    assert second.alarm_request_id == first.alarm_request_id
    assert episodes.record["open"] is True
    assert "pending" not in episodes.record


def test_request_ids_are_derived_from_the_episode_and_never_reused_across_episodes(
    handler, guard, appender, request_for
) -> None:
    guard.latch = present(LATCH)
    first = handler.handle(request_for())
    assert first.alarm_request_id == str(
        uuid.uuid5(module.ALARM_NAMESPACE, f"lab-a|trip|{LATCH['tripped_at']}")
    )
    guard.latch = ABSENT
    release = handler.handle(request_for())
    assert release.alarm_request_id == str(
        uuid.uuid5(module.ALARM_NAMESPACE, f"lab-a|release|{LATCH['tripped_at']}")
    )
    guard.latch = present({**LATCH, "tripped_at": "2026-11-15T05:00:00Z"})
    second = handler.handle(request_for())
    assert second.alarm == "trip"
    assert second.alarm_request_id != first.alarm_request_id


def test_outbox_rows_are_delivered_and_leave_the_outbox(
    handler, guard, appender, request_for
) -> None:
    guard.outbox = [outbox_row(1), outbox_row(2), outbox_row(3)]
    result = handler.handle(request_for())
    assert (result.delivered, result.kept, result.deferred) == (3, 0, 0)
    assert guard.outbox == []
    assert guard.claimed is None
    assert [rid for rid, _ in appender.sent] == [
        f"00000000-0000-4000-8000-{i:012d}" for i in (1, 2, 3)
    ]
    assert all(row.endswith(f"| req={rid}") for rid, row in appender.sent)


def test_a_pending_row_is_kept_byte_for_byte_a_refused_row_is_moved_and_reported(
    handler, guard, appender, request_for
) -> None:
    rows = [outbox_row(1), outbox_row(2), outbox_row(3), outbox_row(4)]
    guard.outbox = list(rows)
    appender.outcomes = {
        "00000000-0000-4000-8000-000000000002": "pending",
        "00000000-0000-4000-8000-000000000003": "refused",
        "00000000-0000-4000-8000-000000000004": "duplicate",
    }
    result = handler.handle(request_for())
    assert (result.delivered, result.kept) == (2, 1)
    assert guard.outbox == [rows[1]]
    assert result.refused == (
        ("00000000-0000-4000-8000-000000000003", "injected refused"),
    )
    assert guard.refused == [(rows[2], "injected refused")]
    assert not result.ok


def test_a_row_without_a_usable_request_id_is_not_sent_and_is_reported(
    handler, guard, appender, request_for
) -> None:
    no_req = OUTBOX_ROW.replace("req=c4671570-cd75-4729-90c1-d794dbfc0eef | ", "")
    bad_req = OUTBOX_ROW.replace("c4671570-cd75-4729-90c1-d794dbfc0eef", "not-a-uuid")
    guard.outbox = [no_req, bad_req]
    result = handler.handle(request_for())
    assert appender.sent == []
    assert result.refused == (("", "no-request-id"), ("", "no-request-id"))
    assert [row for row, _ in guard.refused] == [no_req, bad_req]


def test_the_per_run_bound_defers_the_rest_to_the_outbox(
    handler, guard, appender, request_for
) -> None:
    guard.outbox = [outbox_row(i) for i in range(1, 6)]
    result = handler.handle(request_for(max_outbox_rows_per_run=2))
    assert (result.delivered, result.deferred) == (2, 3)
    assert guard.outbox == [outbox_row(i) for i in (3, 4, 5)]


def test_a_row_the_guard_appends_while_the_outbox_is_claimed_is_not_lost(guard) -> None:
    guard.outbox = [outbox_row(1)]
    assert guard.claim_outbox() == [outbox_row(1)]
    guard.outbox = ["late row"]  # the guard's open(..., "a") after the claim
    guard.settle_outbox(["kept row"], [])
    assert guard.outbox == ["late row", "kept row"]


def test_a_dry_run_decides_and_changes_nothing(
    handler, guard, episodes, appender, request_for
) -> None:
    guard.latch = present(LATCH)
    guard.outbox = [outbox_row(1), outbox_row(2)]
    result = handler.handle(request_for(dry_run=True))
    assert result.alarm == "trip"
    assert result.alarm_status == "dry-run"
    assert result.alarm_row.split(" | ")[1] == "MSG"
    assert appender.sent == []
    assert episodes.record == {}
    assert guard.claimed is None
    assert len(guard.outbox) == 2
    assert result.deferred == 2
    assert result.ok


def test_row_with_req_last_moves_only_the_req_cell() -> None:
    row, request_id = row_with_req_last(OUTBOX_ROW)
    assert request_id == "c4671570-cd75-4729-90c1-d794dbfc0eef"
    assert row.endswith(f" | req={request_id}")
    assert row.count("req=") == 1
    assert row.split(" | ")[:-1] == [
        c for c in OUTBOX_ROW.split(" | ") if not c.startswith("req=")
    ]
    two = OUTBOX_ROW + " | req=00000000-0000-4000-8000-000000000001"
    assert row_with_req_last(two) is None
    assert (
        row_with_req_last(
            OUTBOX_ROW.replace("c4671570-cd75-4729-90c1-d794dbfc0eef", "x")
        )
        is None
    )


@pytest.mark.parametrize(
    ("latch", "episode_open", "expected"),
    [
        (True, False, "trip"),
        (False, True, "release"),
        (True, True, None),
        (False, False, None),
    ],
)
def test_decide_alarm(latch: bool, episode_open: bool, expected: str | None) -> None:
    assert decide_alarm(latch, episode_open) == expected
