# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A lane that ended short with work left is a stopped line on the floor record (OMN-22934).

The rows are the ledger of 2026-11-07, read through the floor effect's own ledger parser: ``pieces-rest-0930`` ended
partial and named its branches (stopped); ``floor-publish-install-1010`` ended blocked asking for a decision but
``floor-publish-install-1025`` claimed the ticket 2 minutes later (taken); ``runners-r2-0930`` and ``landing-owner-0900``
ended partial and name no branch, ask or next step in their row (not stopped); ``ground-state-report`` ended done.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.models.floor_17a.model_floor_17a import ModelGoalScope
from omnimarket.models.model_factory_floor_input import (
    ModelFactoryFloorRequest,
    ModelFloorLedgerRow,
)
from omnimarket.nodes.node_factory_floor_compute.handlers.floor_stopped import (
    UNREAD_SCOPE_CAUSE,
    build_stopped,
)
from omnimarket.nodes.node_factory_floor_compute.handlers.handler_factory_floor import (
    compute_factory_floor,
)
from omnimarket.nodes.node_factory_floor_effect.source_loader import (
    parse_ledger_text,
)
from tests.nodes.node_factory_floor_compute.support import config, request, row

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).parent / "fixtures" / "stopped-lane-ledger-2026-11-07.txt"
READ_AT = datetime(2026, 11, 7, 13, 30, tzinfo=UTC)
SCOPE = ModelGoalScope(ruling="test", in_scope_tickets=("OMN-22478", "OMN-22862"))


def ledger() -> tuple[ModelFloorLedgerRow, ...]:
    return parse_ledger_text(FIXTURE.read_text(encoding="utf-8"))


def floor_request(
    rows: tuple[ModelFloorLedgerRow, ...] | None = None,
    scope: ModelGoalScope | None = SCOPE,
    **config_changes: object,
) -> ModelFactoryFloorRequest:
    made = request(ledger=ledger() if rows is None else rows, goal_scope=scope)
    cfg = made.config
    if config_changes:
        cfg = cfg.model_copy(
            update={"stopped": cfg.stopped.model_copy(update=config_changes)}
        )
    return made.model_copy(update={"generated_at": READ_AT, "config": cfg})


def lanes(result: object) -> list[str]:
    return [s.lane for s in result[0]]  # type: ignore[index]


def causes(result: object) -> dict[str, str]:
    return {s.lane: s.cause for s in result[1]}  # type: ignore[index]


def test_fixture_is_tonights_ledger() -> None:
    assert {r.lane for r in ledger()} == {
        "pieces-rest-0930",
        "floor-publish-install-1010",
        "floor-publish-install-1025",
        "runners-r2-0930",
        "landing-owner-0900",
        "ground-state-report",
    }


def test_golden_a_partial_lane_naming_a_branch_is_listed_with_its_rows_and_report() -> (
    None
):
    stopped, _skipped, cause = build_stopped(floor_request())
    assert cause == ""
    assert [s.lane for s in stopped] == ["pieces-rest-0930"]
    one = stopped[0]
    assert (one.outcome, one.names) == ("partial", ("branch",))
    assert (one.branch, one.head) == ("omn-22478-drift-revision-source", "71b6463")
    assert one.ticket == "OMN-22478"
    assert one.tickets == ()
    assert one.ended_at == "2026-11-07T10:21:09Z"
    assert one.age_minutes == 188
    assert one.report_path == ".claude_scratch/reports/pieces-rest-0930/result.md"
    assert len(one.rows) == 2
    assert one.rows[-1].startswith("2026-11-07T10:21:09Z TERMINAL outcome=partial:")


def test_golden_the_floor_record_lists_it_under_floor_stopped() -> None:
    result = compute_factory_floor(floor_request())
    assert [s.lane for s in result.floor.stopped] == ["pieces-rest-0930"]
    assert result.floor.stopped_cause == ""
    assert {s.lane for s in result.floor.stopped_skipped} == {
        "floor-publish-install-1010",
        "runners-r2-0930",
        "landing-owner-0900",
    }


def test_control_a_lane_that_ended_done_is_not_listed_and_not_skipped() -> None:
    result = build_stopped(floor_request())
    assert "ground-state-report" not in lanes(result)
    assert "ground-state-report" not in causes(result)


def test_control_a_lane_naming_no_branch_ask_or_next_step_is_not_listed() -> None:
    result = build_stopped(floor_request())
    assert causes(result)["runners-r2-0930"] == "names-no-branch-ask-or-next-step"
    assert causes(result)["landing-owner-0900"] == "names-no-branch-ask-or-next-step"


def test_control_a_lane_whose_ticket_a_successor_claimed_is_not_listed() -> None:
    result = build_stopped(floor_request())
    assert "floor-publish-install-1010" not in lanes(result)
    assert (
        causes(result)["floor-publish-install-1010"]
        == "claimed-since:floor-publish-install-1025"
    )


def test_the_blocked_lane_is_listed_when_nobody_claimed_after_it() -> None:
    rows = tuple(r for r in ledger() if r.lane != "floor-publish-install-1025")
    stopped, _skipped, _cause = build_stopped(floor_request(rows))
    blocked = {s.lane: s for s in stopped}["floor-publish-install-1010"]
    assert (blocked.outcome, blocked.ticket) == ("blocked", "OMN-22862")
    assert blocked.names == ("next-step",)


def test_a_lane_outside_the_goal_scope_is_not_listed() -> None:
    narrow = ModelGoalScope(ruling="test", in_scope_tickets=("OMN-22862",))
    result = build_stopped(floor_request(scope=narrow))
    assert "pieces-rest-0930" not in lanes(result)
    assert causes(result)["pieces-rest-0930"] == "out-of-scope"


def test_error_chain_an_unread_goal_scope_lists_nothing_and_says_why() -> None:
    stopped, skipped, cause = build_stopped(floor_request(scope=None))
    assert (stopped, skipped, cause) == ((), (), UNREAD_SCOPE_CAUSE)
    assert compute_factory_floor(floor_request(scope=None)).floor.stopped_cause == (
        UNREAD_SCOPE_CAUSE
    )


def test_a_lane_that_claimed_again_itself_is_not_listed() -> None:
    again = (
        *ledger(),
        ModelFloorLedgerRow(
            ts=datetime(2026, 11, 7, 11, 0, tzinfo=UTC),
            kind="CLAIM",
            lane="pieces-rest-0930",
            tickets=("OMN-22478",),
        ),
    )
    assert "pieces-rest-0930" not in lanes(build_stopped(floor_request(again)))


def test_a_later_claim_on_an_umbrella_ticket_alone_does_not_take_the_work() -> None:
    other = ModelFloorLedgerRow(
        ts=datetime(2026, 11, 7, 11, 0, tzinfo=UTC),
        kind="CLAIM",
        lane="unrelated-1100",
        tickets=("OMN-22478",),
    )
    result = build_stopped(floor_request((*ledger(), other)))
    assert "pieces-rest-0930" in lanes(result)


def test_a_later_claim_naming_the_branch_takes_the_work() -> None:
    other = ModelFloorLedgerRow(
        ts=datetime(2026, 11, 7, 11, 0, tzinfo=UTC),
        kind="CLAIM",
        lane="continue-1100",
        tickets=("OMN-22478",),
        text="continues omn-22478-drift-revision-source",
    )
    result = build_stopped(floor_request((*ledger(), other)))
    assert "pieces-rest-0930" not in lanes(result)
    assert causes(result)["pieces-rest-0930"] == "claimed-since:continue-1100"


def test_a_stop_older_than_the_window_is_not_listed() -> None:
    result = build_stopped(floor_request(max_age_minutes=60))
    assert lanes(result) == []
    assert causes(result) == {}


# The failing controls: each names an injected break, and shows the check turning red without the rule it guards.


def test_break_listing_done_outcomes_lists_the_done_lane() -> None:
    broken = build_stopped(
        floor_request(outcomes=("partial", "blocked", "failed", "done"))
    )
    assert "ground-state-report" in causes(broken) or "ground-state-report" in lanes(
        broken
    )


def test_break_an_empty_next_step_marker_lists_every_lane_that_names_nothing() -> None:
    broken = build_stopped(floor_request(next_step_markers=("",)))
    assert {"runners-r2-0930", "landing-owner-0900"} <= set(lanes(broken))


def test_break_a_stem_that_matches_nothing_cannot_hide_a_successor_on_the_ticket() -> (
    None
):
    # The ticket alone still ties the successor: removing the stem rule does not list the taken lane.
    broken = build_stopped(floor_request(lane_stem_suffix="^$"))
    assert "floor-publish-install-1010" not in lanes(broken)


def test_the_contract_config_loads_the_stopped_block() -> None:
    cfg = config().stopped
    assert cfg.outcomes == ("partial", "blocked", "failed")
    assert row("TERMINAL", 1).kind == "TERMINAL"
