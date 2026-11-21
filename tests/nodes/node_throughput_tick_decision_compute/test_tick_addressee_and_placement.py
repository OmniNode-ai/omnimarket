# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A tick MSG's addressee must be a live lane; a lane is placed only by a CLAIM with host= and run= (OMN-26216).

2026-11-20: the tick wrote MSGs `to=orchestrator-31d9` while the live lane was `orchestrator-31d9-swarm`, so
no MSG had a reader; and a TERMINAL `outcome=completed` was read as a placement.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.handlers.ledger_row_grammar import parse_row
from omnimarket.nodes.node_throughput_tick_decision_compute.handlers.handler_throughput_tick_decision import (
    HandlerThroughputTickDecision,
)
from omnimarket.nodes.node_throughput_tick_decision_compute.models.model_throughput_tick_decision import (
    ModelThroughputTickRequest,
)

pytestmark = pytest.mark.unit

NOW = "2026-11-20T16:00:00Z"
LIVE = ["orchestrator-31d9-swarm", "landing-controller"]
# The tick's own row shape, as the ledger holds it (the 2026-11-20T15:58:25Z row, cells shortened).
TICK_ROW = (
    "2026-11-20T15:58:25Z | MSG | from=launchd-throughput-tick | to=orchestrator-31d9"
    " | id=2026-11-20T15:58:25Z-launchd-throughput-tick | ticket=OMN-20800 | throughput-tick is not OK"
)


def run(**overrides: Any) -> Any:
    payload: dict[str, Any] = {"now": NOW, "launchctl": "not_loaded", **overrides}
    return HandlerThroughputTickDecision().handle(
        ModelThroughputTickRequest.model_validate(payload)
    )


def message_from_row(row: str, *, acked: bool = False) -> dict[str, Any]:
    parsed = parse_row(row, types=frozenset({"MSG"}))
    assert parsed is not None
    return {
        "id": parsed.value("id"),
        "to": parsed.value("to"),
        "sent_at": parsed.stamp,
        "acked": acked,
    }


def alarm_loops(result: Any) -> list[str]:
    return [a.loop for a in result.alarms]


# --- AC2 ----------------------------------------------------------------------------------------


def test_tick_addressee_resolves_a_stem_of_the_live_lane_is_an_alarm() -> None:
    result = run(tick_messages=[message_from_row(TICK_ROW)], live_lanes=LIVE)
    assert alarm_loops(result) == ["tick-addressee:orchestrator-31d9"]
    [line] = [x for x in result.lines if x.startswith("ALARM tick-addressee:")]
    assert "not a live lane" in line
    assert "tick-addressees" in result.checked
    assert result.exit_code == 1


def test_tick_addressee_resolves_the_full_lane_name_is_no_finding() -> None:
    row = TICK_ROW.replace("to=orchestrator-31d9 ", "to=orchestrator-31d9-swarm ")
    result = run(
        tick_messages=[message_from_row(row, acked=True)],
        live_lanes=LIVE,
    )
    assert result.alarms == []
    assert "tick-addressees" in result.checked


def test_tick_addressee_resolves_every_name_of_a_comma_list() -> None:
    row = TICK_ROW.replace("to=orchestrator-31d9 ", "to=landing-controller,gone-lane ")
    result = run(tick_messages=[message_from_row(row)], live_lanes=LIVE)
    assert alarm_loops(result) == ["tick-addressee:gone-lane"]


def test_tick_addressee_resolves_without_facts_skips_the_check() -> None:
    """Positive control: no tick MSG facts and no live-lane facts leave the check off, not green."""
    assert "tick-addressees" not in run().checked
    assert "tick-addressees" not in run(live_lanes=LIVE).checked


@pytest.mark.parametrize(
    ("minutes", "alarm"),
    [(21, False), (22, False), (23, True)],
)
def test_tick_addressee_resolves_unacked_after_two_ticks_opens_the_cause(
    minutes: int, alarm: bool
) -> None:
    sent = f"2026-11-20T{15 if minutes <= 60 else 14}:{60 - minutes:02d}:00Z"
    row = TICK_ROW.replace("to=orchestrator-31d9 ", "to=orchestrator-31d9-swarm ")
    row = row.replace("2026-11-20T15:58:25Z", sent)
    result = run(tick_messages=[message_from_row(row)], live_lanes=LIVE)
    expected = ["tick-unacked:orchestrator-31d9-swarm"] if alarm else []
    assert alarm_loops(result) == expected
    if alarm:
        [line] = [x for x in result.lines if x.startswith("ALARM tick-unacked:")]
        assert "open the cause lane" in line


def test_tick_addressee_resolves_an_acked_message_is_never_an_alarm() -> None:
    row = TICK_ROW.replace("to=orchestrator-31d9 ", "to=orchestrator-31d9-swarm ")
    row = row.replace("2026-11-20T15:58:25Z", "2026-11-20T10:00:00Z")
    result = run(tick_messages=[message_from_row(row, acked=True)], live_lanes=LIVE)
    assert result.alarms == []


# --- AC3 ----------------------------------------------------------------------------------------


def lane(name: str, dispatched: str, **extra: Any) -> dict[str, Any]:
    return {"lane": name, "dispatched_at": dispatched, **extra}


def test_placed_needs_claim_with_host_and_run_inside_five_minutes() -> None:
    result = run(
        dispatched_lanes=[
            lane(
                "fix-a",
                "2026-11-20T15:40:00Z",
                claim_host="h201",
                claim_run_id="rlane-fix-a-1",
                claim_at="2026-11-20T15:45:00Z",
            )
        ]
    )
    assert result.alarms == []
    assert not [x for x in result.lines if "unplaced-lane" in x]
    assert result.status_rows == []


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"claim_host": "h201", "claim_at": "2026-11-20T15:41:00Z"},
        {"claim_run_id": "rlane-fix-a-1", "claim_at": "2026-11-20T15:41:00Z"},
        {"claim_host": "h201", "claim_run_id": "rlane-fix-a-1"},
        {
            "claim_host": "h201",
            "claim_run_id": "rlane-fix-a-1",
            "claim_at": "2026-11-20T15:45:01Z",
        },
    ],
    ids=["no-claim", "no-run", "no-host", "claim-time-unread", "claim-after-window"],
)
def test_placed_needs_claim_otherwise_the_tick_writes_status_unplaced(
    extra: dict[str, Any],
) -> None:
    result = run(dispatched_lanes=[lane("fix-a", "2026-11-20T15:40:00Z", **extra)])
    [row] = result.status_rows
    parsed = parse_row(row, types=frozenset({"STATUS"}))
    assert parsed is not None
    assert parsed.stamp == NOW
    assert parsed.value("lane") == "fix-a"
    assert parsed.value("state") == "unplaced"
    assert alarm_loops(result) == ["unplaced-lane:fix-a"]


def test_placed_needs_claim_a_terminal_completed_is_not_a_placement() -> None:
    result = run(
        dispatched_lanes=[
            lane("fix-a", "2026-11-20T15:40:00Z", terminal_outcome="completed")
        ]
    )
    [row] = result.status_rows
    assert "outcome=completed is not a placement" in row
    assert alarm_loops(result) == ["unplaced-lane:fix-a"]


def test_placed_needs_claim_a_young_lane_waits_out_the_window_with_no_row() -> None:
    result = run(dispatched_lanes=[lane("fix-a", "2026-11-20T15:56:00Z")])
    assert result.status_rows == []
    assert result.alarms == []
    assert [x for x in result.lines if x.startswith("NOTE unplaced-lane:fix-a")]
