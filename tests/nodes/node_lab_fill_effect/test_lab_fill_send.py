# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab-fill send step from the node: render, send gate, brief check, launch (OMN-23277).

The old lab-fill.js send step sent a lane for a ticket whose implementation had merged (about 45 fill lanes on
2026-11-09), and sent a brief whose facts need h200 to a host that cannot reach h200. Each defect has a
test that fails with its check removed (see the named injected breaks in the pull request).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.models.lab_fill.model_lab_fill_closer import ModelLabFillBuiltReport
from omnimarket.nodes.node_lab_fill_effect.handlers.handler_lab_fill_launch import (
    HandlerLabFillLaunch,
)
from omnimarket.nodes.node_lab_fill_effect.handlers.handler_lab_fill_send import (
    HandlerLabFillSend,
)
from omnimarket.nodes.node_lab_fill_effect.handlers.handler_lab_fill_send_gate import (
    HandlerLabFillSendGate,
    brief_needs_hosts,
)
from omnimarket.nodes.node_lab_fill_effect.models.model_lab_fill_send import (
    ModelLabFillSendRequest,
)
from omnimarket.nodes.node_lab_fill_effect.models.model_lab_fill_send_gate import (
    CLOSER_NODE,
    EnumLabFillSendVerdict,
    ModelLabFillSendGateRequest,
)
from omnimarket.nodes.node_lab_fill_plan_compute.models import ModelLabFillDispatchItem
from omnimarket.nodes.node_lab_fill_plan_compute.models.model_lab_fill_lane_render import (
    ModelLabFillLaneRenderRequest,
    ModelLabFillRenderConfig,
)

from .fakes import (
    START,
    FakeApproved,
    FakeBlocks,
    FakeChecks,
    FakeClock,
    FakeMerged,
    FakeRunner,
)

pytestmark = pytest.mark.unit


def _request(
    tmp_path: Path,
    *,
    kind: str = "ticket",
    title: str = "the ticket title",
    host: str = "h053",
    pinned: bool = True,
    host_reach: dict[str, tuple[str, ...]] | None = None,
) -> ModelLabFillSendRequest:
    item = ModelLabFillDispatchItem(
        lane="lab-fill-omn9-x",
        kind=kind,
        id="row-1",
        ticket="OMN-9",
        pr="",
        repo="repo_a",
        ref="main",
        title=title,
        updated_at="2026-11-09T00:00:00Z",
        host=host,
        pinned=pinned,
        engine="sonnet",
        route="ROUTE: band=B3 score=10",
    )
    config = ModelLabFillRenderConfig(
        pillar="landing",
        parent_lane="lab-fill",
        parent_ticket="OMN-23277",
        run_key="2026-11-09",
        authority_ruling="2026-10-10T04:08:23Z",
        authority_lane="ruling-idle-lab-9f8a",
    )
    return ModelLabFillSendRequest(
        render=ModelLabFillLaneRenderRequest(item=item, config=config),
        brief_dir=str(tmp_path),
        ledger_path="/l",
        window_end_epoch_s=int(START) + 400,
        host_reach=host_reach or {},
    )


def _sender(runner: FakeRunner, merged: FakeMerged | None = None) -> HandlerLabFillSend:
    return HandlerLabFillSend(
        HandlerLabFillLaunch(
            checks=FakeChecks(),
            approved=FakeApproved(),
            blocks=FakeBlocks(),
            runner=runner,
            clock=FakeClock(),
            merged=merged if merged is not None else FakeMerged(),
        )
    )


def test_lab_fill_send_renders_the_brief_and_launches_the_lane(tmp_path: Path) -> None:
    runner = FakeRunner()
    result = _sender(runner).handle(_request(tmp_path))
    assert (result.detached, result.skipped) == (True, "")
    assert result.receipt == "/state/x/receipt.json"
    assert len(runner.calls) == 1
    args = runner.calls[0][0]
    assert args[args.index("--lane") + 1] == "lab-fill-omn9-x"
    assert args[args.index("--host") + 1] == "h053"
    brief = (tmp_path / "lab-fill-omn9-x.md").read_text(encoding="utf-8")
    assert brief.startswith("# Lane lab-fill-omn9-x")
    assert "TASK: Work OMN-9: the ticket title." in brief
    assert "3a.9 Delegation" in brief


def test_lab_fill_send_brief_check_refusal_is_reported_not_retried(
    tmp_path: Path,
) -> None:
    from omnimarket.nodes.node_lab_fill_effect.protocols import LabFillCommandOutcome

    runner = FakeRunner(
        outcome=LabFillCommandOutcome(
            2, "REMOTE-LANE-MINT-REFUSED a lab lane may not mint a ticket\n", ""
        )
    )
    result = _sender(runner).handle(_request(tmp_path))
    assert (result.detached, result.skipped) == (False, "brief-check-refused")
    assert len(runner.calls) == 1


def test_merged_ticket_skip_routes_to_the_closer_and_sends_no_lane(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    merged = FakeMerged(by_ticket={"OMN-9": ["omnimarket#5400", "omnimarket#5401"]})
    result = _sender(runner, merged).handle(_request(tmp_path))
    assert result.detached is False
    assert result.skipped == "implementation-merged"
    assert result.routed_to == CLOSER_NODE
    assert result.merged_prs == ("omnimarket#5400", "omnimarket#5401")
    assert "omnimarket#5400,omnimarket#5401" in result.detail
    assert runner.calls == []
    assert not (tmp_path / "lab-fill-omn9-x.md").exists()


def test_merged_ticket_skip_leaves_a_ticket_with_no_merged_pr_alone(
    tmp_path: Path,
) -> None:
    merged = FakeMerged(by_ticket={"OMN-10": ["omnimarket#1"]})
    result = _sender(FakeRunner(), merged).handle(_request(tmp_path))
    assert result.detached is True
    assert merged.calls == ["OMN-9"]


@pytest.mark.parametrize("kind", ["pr-red", "pr-stalled", "pr-land", "diagnose"])
def test_merged_ticket_skip_does_not_stop_a_pr_lane_or_the_fills_own_lane(
    kind: str,
) -> None:
    gate = HandlerLabFillSendGate().handle(
        ModelLabFillSendGateRequest(
            ticket="OMN-9", kind=kind, brief="TASK: x", merged_prs=("repo_a#1",)
        )
    )
    assert gate.verdict is EnumLabFillSendVerdict.SEND


def test_merged_ticket_skip_is_not_taken_on_an_unread_merge_state(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    result = _sender(runner, FakeMerged(fail="unreadable-from-snapshot")).handle(
        _request(tmp_path)
    )
    assert (result.detached, result.skipped) == (False, "merged-state-unreadable")
    assert runner.calls == []


def test_h200_bound_brief_is_not_sent_to_a_host_that_cannot_reach_h200(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    result = _sender(runner).handle(
        _request(tmp_path, title="read the h200 drainer reading", host="h053")
    )
    assert (result.detached, result.skipped) == (False, "fact-host-unreachable")
    assert "h053 does not reach it" in result.detail
    assert runner.calls == []


def test_h200_bound_brief_goes_to_a_host_that_reaches_h200(tmp_path: Path) -> None:
    runner = FakeRunner()
    result = _sender(runner).handle(
        _request(
            tmp_path,
            title="read the h200 drainer reading",
            host="h053",
            host_reach={"h053": ("h200",)},
        )
    )
    assert result.detached is True
    assert runner.calls[0][0][runner.calls[0][0].index("--host") + 1] == "h053"


def test_h200_bound_brief_goes_to_h200_itself(tmp_path: Path) -> None:
    runner = FakeRunner()
    result = _sender(runner).handle(
        _request(tmp_path, title="read the h200 drainer reading", host="h200")
    )
    assert result.detached is True


def test_h200_bound_unpinned_brief_is_pinned_to_a_reaching_host(tmp_path: Path) -> None:
    runner = FakeRunner()
    result = _sender(runner).handle(
        _request(
            tmp_path,
            title="read the h200 drainer reading",
            pinned=False,
            host_reach={"h101": ("h200",), "h053": ()},
        )
    )
    assert result.detached is True
    args = runner.calls[0][0]
    assert args[args.index("--host") + 1] == "h101"


def test_h200_bound_brief_without_a_reading_of_reach_is_refused_everywhere() -> None:
    gate = HandlerLabFillSendGate().handle(
        ModelLabFillSendGateRequest(
            ticket="OMN-9",
            kind="ticket",
            brief="TASK: needs the h200 llama server",
            host="h101",
            host_reach={},
        )
    )
    assert gate.verdict is EnumLabFillSendVerdict.FACT_HOST_UNREACHABLE


def test_h200_bound_brief_naming_h2000_or_a_longer_name_is_not_h200() -> None:
    assert brief_needs_hosts("host h2000 and h200x and my-h200", ("h200",), ()) == ()
    assert brief_needs_hosts("on h200.", ("h200",), ()) == ("h200",)
    assert brief_needs_hosts("plain", ("h200",), ("h200",)) == ("h200",)


def test_h200_bound_brief_with_no_h200_fact_is_sent_to_any_host(tmp_path: Path) -> None:
    result = _sender(FakeRunner()).handle(_request(tmp_path, host="h053"))
    assert result.detached is True


def test_a_dod_verify_lane_is_the_closers_work_so_the_gate_sends_none() -> None:
    """OMN-23376: the closer verifies a merged ticket; a lane that rebuilt a worktree and a venv to find it built was the waste."""
    gate = HandlerLabFillSendGate().handle(
        ModelLabFillSendGateRequest(
            ticket="OMN-9",
            kind="dod-verify",
            brief="TASK: x",
            merged_prs=("repo_a#1",),
        )
    )
    assert gate.verdict is EnumLabFillSendVerdict.IMPLEMENTATION_MERGED
    assert gate.routed_to == CLOSER_NODE


def test_an_already_built_report_with_passing_falsifiers_sends_no_lane(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    built = (
        ModelLabFillBuiltReport(
            ticket="OMN-9",
            lane="fill-ticket-OMN-9-20261109T1419Z",
            reported_at="2026-11-09T14:29:11Z",
            falsifiers_passed=True,
            evidence="pytest -k ac1 exit 0 on dev a4798bc9d",
        ),
    )
    result = _sender(runner, FakeMerged(built=built)).handle(_request(tmp_path))
    assert (result.detached, result.skipped) == (False, "already-built")
    assert result.routed_to == CLOSER_NODE
    assert "pytest -k ac1 exit 0 on dev a4798bc9d" in result.detail
    assert runner.calls == []


def test_an_already_built_report_with_failing_falsifiers_does_not_stop_the_lane(
    tmp_path: Path,
) -> None:
    built = (
        ModelLabFillBuiltReport(
            ticket="OMN-9", lane="x", reported_at="t", falsifiers_passed=False
        ),
    )
    result = _sender(FakeRunner(), FakeMerged(built=built)).handle(_request(tmp_path))
    assert result.detached is True
