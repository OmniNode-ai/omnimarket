# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: a lab-fill tick that cannot read or launch says why and dispatches nothing more (OMN-17427)."""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_lab_fill_effect.handlers import (
    HandlerLabFillLaunch,
    HandlerLabFillTick,
)
from omnimarket.nodes.node_lab_fill_effect.models import (
    ModelLabFillTickHost,
    ModelLabFillTickRequest,
)
from omnimarket.nodes.node_lab_fill_effect.protocols import LabFillClaimFacts

from .fakes import (
    FakeApproved,
    FakeBlocks,
    FakeChecks,
    FakeClock,
    FakeOwners,
    FakeRunner,
)


def _row(n: int) -> dict[str, object]:
    return {
        "id": f"row-{n}",
        "kind": "wiring",
        "ticket": f"OMN-{n}",
        "repo": "omnimarket",
        "goal": f"goal {n}",
        "acceptance_check": f"check {n}",
    }


def _request(tmp_path: Path, free: int = 2) -> ModelLabFillTickRequest:
    return ModelLabFillTickRequest(
        run_key="2026-10-17T0930Z",
        hosts=(ModelLabFillTickHost(host="h201", free=free),),
        ledger_path="/l",
        approved_work_path="/a",
        brief_dir=str(tmp_path / "briefs"),
        operator_id="op",
        parent="orchestrator-bb1a",
        parent_ticket="OMN-17427",
        authority_ruling="2026-10-17T09:03:06Z",
        authority_lane="orchestrator-bb1a",
    )


def _handler(
    approved: FakeApproved,
    *,
    owners: FakeOwners | None = None,
    runner: FakeRunner | None = None,
    blocks: FakeBlocks | None = None,
) -> tuple[HandlerLabFillTick, FakeRunner]:
    runner = runner or FakeRunner()
    clock = FakeClock()
    owners = owners or FakeOwners(
        claims_facts=LabFillClaimFacts(index={}, open_claims=[], staleness_hours=12)
    )
    launch = HandlerLabFillLaunch(
        checks=FakeChecks(),
        approved=approved,
        blocks=blocks or FakeBlocks(),
        runner=runner,
        clock=clock,
    )
    return (
        HandlerLabFillTick(
            approved=approved, owners=owners, launch=launch, clock=clock
        ),
        runner,
    )


def _approved(rows: list[dict[str, object]], fail: str = "") -> FakeApproved:
    return FakeApproved(rows={str(r["id"]): r for r in rows}, all_rows=rows, fail=fail)


def test_unreadable_approved_list_dispatches_nothing(tmp_path: Path) -> None:
    handler, runner = _handler(_approved([], fail="unreadable: no such file"))
    result = handler.handle(_request(tmp_path))
    assert result.outcome == "approved-work-unreadable"
    assert "no such file" in result.detail
    assert result.dispatched == ()
    assert runner.calls == []


def test_no_headroom_reads_nothing_and_says_so(tmp_path: Path) -> None:
    approved = _approved([_row(1)])
    handler, runner = _handler(approved)
    result = handler.handle(_request(tmp_path, free=0))
    assert result.outcome == "no-headroom"
    assert result.free_slots == 0
    assert runner.calls == []


def test_unreadable_owner_is_never_free(tmp_path: Path) -> None:
    owners = FakeOwners(fail={"claims": "claim index unreadable"})
    handler, runner = _handler(_approved([_row(1), _row(2)]), owners=owners)
    result = handler.handle(_request(tmp_path))
    assert result.outcome == "nothing-dispatched"
    assert {s.reason for s in result.skipped} == {"owned-unreadable"}
    assert runner.calls == []


def test_runner_failure_stops_the_tick_and_names_the_rest(tmp_path: Path) -> None:
    runner = FakeRunner(fail="runner not started: ENOENT")
    handler, _ = _handler(_approved([_row(1), _row(2), _row(3)]), runner=runner)
    result = handler.handle(_request(tmp_path))
    assert result.outcome == "nothing-dispatched"
    assert [(s.ticket, s.reason) for s in result.skipped] == [
        ("OMN-1", "runner-unavailable")
    ]
    # a systemic failure does not burn the remaining candidates
    assert result.untried == ("OMN-2", "OMN-3")
    assert len(runner.calls) == 1
    assert result.stopped == "runner-unavailable"


def test_unreadable_standing_rules_stop_the_tick(tmp_path: Path) -> None:
    handler, runner = _handler(
        _approved([_row(1), _row(2)]), blocks=FakeBlocks(fail="lane_rules_block: gone")
    )
    result = handler.handle(_request(tmp_path))
    assert result.stopped == "brief-rules-unreadable"
    assert result.untried == ("OMN-2",)
    assert runner.calls == []


def test_every_candidate_refused_leaves_slots_idle_with_reasons(tmp_path: Path) -> None:
    approved = _approved([_row(1), _row(2)])
    approved.rows = {}  # each launch's by-id read now misses: approved-row-unresolved
    handler, runner = _handler(approved)
    result = handler.handle(_request(tmp_path))
    assert result.outcome == "nothing-dispatched"
    assert [s.reason for s in result.skipped] == [
        "approved-row-unresolved",
        "approved-row-unresolved",
    ]
    assert result.untried == ()
    assert runner.calls == []
