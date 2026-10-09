# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: every failure of a port is named in a typed result, never a silence or a guess (OMN-20668)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.models.lab_fill import ModelLabFillApprovedRow, ModelLabFillLaunch
from omnimarket.models.work_ledger_append import EnumWorkLedgerAppendStatus
from omnimarket.nodes.node_lab_fill_effect.handlers import (
    HandlerLabFillFallbackHost,
    HandlerLabFillLaunch,
    HandlerLabFillOwnerFacts,
    HandlerLabFillProbe,
    HandlerLabFillReceipts,
    HandlerLabFillStatus,
)
from omnimarket.nodes.node_lab_fill_effect.handlers.handler_lab_fill_launch import (
    resolve_approved_row,
)
from omnimarket.nodes.node_lab_fill_effect.models import (
    ModelLabFillFallbackHostRequest,
    ModelLabFillLaunchRequest,
    ModelLabFillOwnerFactsRequest,
    ModelLabFillOwnerLane,
    ModelLabFillProbeRequest,
    ModelLabFillReceiptLane,
    ModelLabFillReceiptsRequest,
    ModelLabFillStatusWriteRequest,
)
from omnimarket.nodes.node_lab_fill_effect.protocols import (
    LabFillClaimFacts,
    LabFillCommandOutcome,
    LabFillPortError,
    ProtocolLabFillApprovedWork,
    ProtocolLabFillBriefBlocks,
    ProtocolLabFillLaneLauncher,
    ProtocolLabFillLedgerReader,
    ProtocolLabFillLiveChecks,
    ProtocolLabFillPlacementReader,
    ProtocolLabFillResultWriter,
    ProtocolLabFillStatusAppender,
)

from .fakes import (
    START,
    FakeAppender,
    FakeApproved,
    FakeBlocks,
    FakeChecks,
    FakeClock,
    FakeLedgerReader,
    FakeOwners,
    FakePlacement,
    FakeReceipts,
    FakeRunner,
    FakeWriter,
)

MARKER = "APPROVED-ROW r1 (unresolved)"
ROW = {
    "kind": "wiring",
    "ticket": "OMN-7",
    "goal": "Wire it",
    "acceptance_check": "It runs",
}


def _probe(
    *,
    placement: ProtocolLabFillPlacementReader | None = None,
    ledger: ProtocolLabFillLedgerReader | None = None,
    approved: ProtocolLabFillApprovedWork | None = None,
) -> HandlerLabFillProbe:
    return HandlerLabFillProbe(
        placement=placement
        if placement is not None
        else FakePlacement([{"name": "h1"}]),
        ledger=ledger if ledger is not None else FakeLedgerReader(),
        approved=approved if approved is not None else FakeApproved(),
        clock=FakeClock(),
    )


def _probe_request(
    *, force: bool = False, approved_work_path: str = ""
) -> ModelLabFillProbeRequest:
    return ModelLabFillProbeRequest(
        run_key="2026-10-09T0100Z",
        ledger_path="/l",
        force=force,
        approved_work_path=approved_work_path,
    )


def test_probe_short_circuits_when_the_fire_is_already_delivered() -> None:
    result = _probe(ledger=FakeLedgerReader(delivered="2026-10-09T01:00:05Z")).handle(
        _probe_request()
    )
    assert (result.outcome, result.precheck_evidence, result.readings) == (
        "already-delivered",
        "2026-10-09T01:00:05Z",
        (),
    )


def test_probe_force_ignores_a_delivered_row() -> None:
    result = _probe(ledger=FakeLedgerReader(delivered="2026-10-09T01:00:05Z")).handle(
        _probe_request(force=True)
    )
    assert result.outcome == "read"


def test_probe_runs_on_when_the_precheck_cannot_read_the_ledger_and_says_so() -> None:
    result = _probe(ledger=FakeLedgerReader(fail="ledger unreadable: ENOENT")).handle(
        _probe_request()
    )
    assert result.outcome == "read"
    assert "precheck: ledger unreadable: ENOENT" in result.notes


def test_probe_names_an_unreadable_pool_instead_of_an_empty_one() -> None:
    result = _probe(placement=FakePlacement(fail="pool read failed: boom")).handle(
        _probe_request()
    )
    assert result.outcome == "unreadable"
    assert result.readings == ()
    assert "pool: pool read failed: boom" in result.notes


def test_probe_depth_is_none_not_zero_when_the_list_cannot_be_read() -> None:
    result = _probe(approved=FakeApproved(list_depth=None)).handle(
        _probe_request(approved_work_path="/a")
    )
    assert result.approved_work_depth is None


@pytest.mark.parametrize("run_key", ["2026-10-09T01:00Z", "today", ""])
def test_a_malformed_run_key_is_refused_at_the_model(run_key: str) -> None:
    with pytest.raises(ValidationError):
        ModelLabFillProbeRequest(run_key=run_key, ledger_path="/l")


def test_owner_facts_carry_each_failed_source_as_text_and_skip_unneeded_sources() -> (
    None
):
    owners = FakeOwners(
        fail={
            "claims": "claim bridge exit 1",
            "pr_claims": "exit 2 bad",
            "watcher": "unreadable-from-snapshot",
        }
    )
    facts = HandlerLabFillOwnerFacts(reader=owners, clock=FakeClock()).handle(
        ModelLabFillOwnerFactsRequest(
            lanes=(
                ModelLabFillOwnerLane(
                    lane="a", ticket="OMN-1", pr="r#1", kind="pr-red"
                ),
                ModelLabFillOwnerLane(lane="b", ticket="OMN-2", kind="process-fix"),
            ),
            ledger_path="/l",
        )
    )
    assert (
        facts.claim_index
        == facts.ledger_claims
        == "LabFillPortError:claim bridge exit 1"
    )
    assert facts.pr_claims == "LabFillPortError:exit 2 bad"
    assert facts.watcher_merged == "LabFillPortError:unreadable-from-snapshot"

    quiet = FakeOwners(
        claims_facts=LabFillClaimFacts(index={}, open_claims=[], staleness_hours=12)
    )
    HandlerLabFillOwnerFacts(reader=quiet, clock=FakeClock()).handle(
        ModelLabFillOwnerFactsRequest(
            lanes=(ModelLabFillOwnerLane(lane="a", ticket="OMN-1", kind="ticket"),),
            ledger_path="/l",
        )
    )
    assert quiet.called == ["claims"]


def test_owner_facts_keep_the_two_claim_readers_failures_separate() -> None:
    owners = FakeOwners(
        claims_facts=LabFillClaimFacts(
            index="index: boom", open_claims=[], staleness_hours=12
        )
    )
    facts = HandlerLabFillOwnerFacts(reader=owners, clock=FakeClock()).handle(
        ModelLabFillOwnerFactsRequest(
            lanes=(ModelLabFillOwnerLane(lane="a", ticket="OMN-1"),), ledger_path="/l"
        )
    )
    assert facts.claim_index == "index: boom"
    assert facts.ledger_claims == ()


def _launch_request(
    tmp_path: Path,
    *,
    launch: ModelLabFillLaunch | None = None,
    kind: str = "pr-red",
    brief: str = "# Lane\nbody\n",
    window_end_epoch_s: int = int(START) + 400,
    lanes_left: int = 1,
) -> ModelLabFillLaunchRequest:
    return ModelLabFillLaunchRequest(
        launch=launch
        or ModelLabFillLaunch(
            lane="lab-fill-omn7-x",
            ticket="OMN-7",
            engine="codex",
            parent="orch",
            timeout_min=30,
            pr="repo#9",
        ),
        kind=kind,
        brief=brief,
        brief_dir=str(tmp_path),
        ledger_path="/l",
        window_end_epoch_s=window_end_epoch_s,
        lanes_left=lanes_left,
    )


def _launcher(
    *,
    checks: ProtocolLabFillLiveChecks | None = None,
    approved: ProtocolLabFillApprovedWork | None = None,
    blocks: ProtocolLabFillBriefBlocks | None = None,
    runner: ProtocolLabFillLaneLauncher | None = None,
) -> HandlerLabFillLaunch:
    return HandlerLabFillLaunch(
        checks=checks if checks is not None else FakeChecks(),
        approved=approved if approved is not None else FakeApproved(),
        blocks=blocks if blocks is not None else FakeBlocks(),
        runner=runner if runner is not None else FakeRunner(),
        clock=FakeClock(),
    )


def test_launch_does_not_start_a_lane_past_its_slice(tmp_path: Path) -> None:
    runner = FakeRunner()
    result = _launcher(runner=runner).handle(
        _launch_request(tmp_path, window_end_epoch_s=int(START) + 20, lanes_left=2)
    )
    assert (result.detached, result.skipped, result.elapsed_s) == (False, "deadline", 0)
    assert runner.calls == []
    assert not (tmp_path / "lab-fill-omn7-x.md").exists()


@pytest.mark.parametrize(
    ("answer", "fail", "expected"),
    [
        (("assigned-other", "assignee x"), "", "assigned-other"),
        (("held", "hold:wip"), "", "held"),
        (("fenced", "OMN-7"), "", "fenced"),
        (("", ""), "Linear unreadable: URLError", "ticket-unreadable"),
    ],
)
def test_launch_names_the_live_check_that_stopped_it(
    tmp_path: Path, answer: tuple[str, str], fail: str, expected: str
) -> None:
    runner = FakeRunner()
    result = _launcher(
        checks=FakeChecks(answer=answer, fail=fail), runner=runner
    ).handle(_launch_request(tmp_path))
    assert (result.detached, result.skipped) == (False, expected)
    assert runner.calls == []


@pytest.mark.parametrize(
    ("row", "reason"),
    [
        ({**ROW, "ticket": "OMN-8"}, "kind or ticket changed"),
        ({**ROW, "kind": "partial-node"}, "kind or ticket changed"),
        ({**ROW, "goal": "two\nlines"}, "invalid goal"),
        ({**ROW, "acceptance_check": "  "}, "invalid acceptance_check"),
        ({**ROW, "blocked_until": "OMN-9 merges"}, "blocked"),
    ],
)
def test_approved_row_changed_since_selection_is_refused(
    row: dict[str, object], reason: str
) -> None:
    ref = ModelLabFillApprovedRow(id="r1", kind="wiring", ticket="OMN-7", marker=MARKER)
    with pytest.raises(LabFillPortError, match=f"APPROVED-ROW r1 {reason}"):
        resolve_approved_row(f"a\n{MARKER}\nb\n", ref, row)


@pytest.mark.parametrize("brief", ["no marker here\n", f"{MARKER}\n{MARKER}\n"])
def test_approved_row_marker_must_appear_exactly_once(brief: str) -> None:
    ref = ModelLabFillApprovedRow(id="r1", kind="wiring", ticket="OMN-7", marker=MARKER)
    with pytest.raises(LabFillPortError, match="missing or duplicate marker"):
        resolve_approved_row(brief, ref, ROW)


def test_approved_row_remaining_work_is_carried_into_the_brief() -> None:
    ref = ModelLabFillApprovedRow(id="r1", kind="wiring", ticket="OMN-7", marker=MARKER)
    out = resolve_approved_row(
        f"a\n{MARKER}\nb\n", ref, {**ROW, "remaining": " the rest "}
    )
    assert (
        out
        == "a\nGOAL: Wire it\nACCEPTANCE CHECK: It runs\nALREADY SHIPPED, REMAINS: the rest\nb\n"
    )


def test_launch_refuses_an_unresolvable_approved_row_without_dispatching(
    tmp_path: Path,
) -> None:
    launch = ModelLabFillLaunch(
        lane="lab-fill-omn7-x",
        ticket="OMN-7",
        engine="sonnet",
        parent="orch",
        timeout_min=30,
        approved_row=ModelLabFillApprovedRow(
            id="r1", kind="wiring", ticket="OMN-7", marker=MARKER
        ),
    )
    runner = FakeRunner()
    result = _launcher(
        approved=FakeApproved(fail="unreadable: ENOENT"), runner=runner
    ).handle(
        _launch_request(tmp_path, launch=launch, kind="wiring", brief=f"{MARKER}\n")
    )
    assert (result.detached, result.skipped) == (False, "approved-row-unresolved")
    assert "unreadable: ENOENT" in result.detail
    assert runner.calls == []


def test_launch_refuses_when_the_standing_rules_cannot_be_read(tmp_path: Path) -> None:
    runner = FakeRunner()
    result = _launcher(
        blocks=FakeBlocks(fail="brief standing rules heading is missing"), runner=runner
    ).handle(_launch_request(tmp_path))
    assert (result.detached, result.skipped) == (False, "brief-rules-unreadable")
    assert runner.calls == []


def test_launch_stops_at_the_slice_when_the_runner_times_out(tmp_path: Path) -> None:
    result = _launcher(
        runner=FakeRunner(outcome=LabFillCommandOutcome(124, "", "timed out"))
    ).handle(_launch_request(tmp_path))
    assert (result.detached, result.skipped) == (False, "lane-timeout:0s")
    assert result.brief_has_delegation


def test_launch_reports_the_last_lines_when_the_runner_does_not_detach(
    tmp_path: Path,
) -> None:
    outcome = LabFillCommandOutcome(
        2, "line one\nno-host: all full\nusage refused\n", ""
    )
    result = _launcher(runner=FakeRunner(outcome=outcome)).handle(
        _launch_request(tmp_path)
    )
    assert (result.detached, result.receipt, result.detail) == (
        False,
        "",
        "no-host: all full / usage refused",
    )


def test_launch_does_not_call_exit_zero_without_a_detached_receipt_a_launch(
    tmp_path: Path,
) -> None:
    result = _launcher(
        runner=FakeRunner(outcome=LabFillCommandOutcome(0, "started\n", ""))
    ).handle(_launch_request(tmp_path))
    assert result.detached is False


def test_launch_names_a_runner_that_could_not_start(tmp_path: Path) -> None:
    result = _launcher(runner=FakeRunner(fail="runner not started: ENOENT")).handle(
        _launch_request(tmp_path)
    )
    assert (result.detached, result.skipped) == (False, "runner-unavailable")


def test_launch_of_an_unpinned_lane_carries_no_host_and_no_ref_without_a_repo(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    launch = ModelLabFillLaunch(
        lane="lab-fill-omn7-x",
        ticket="OMN-7",
        engine="sonnet",
        parent="orch",
        timeout_min=30,
        ref="main",
    )
    _launcher(runner=runner).handle(_launch_request(tmp_path, launch=launch))
    args = runner.calls[0][0]
    assert "--host" not in args
    assert "--ref" not in args
    assert "--repo" not in args
    assert args[-1] == "--detach"


def test_receipts_wait_for_a_codex_lane_until_final_then_stop() -> None:
    clock = FakeClock()
    reader = FakeReceipts(
        {
            "/r": [
                {"status": "running", "host": "h1", "final": False},
                {"status": "running", "host": "h1", "final": True, "exit_code": 0},
            ]
        }
    )
    result = HandlerLabFillReceipts(reader=reader, clock=clock).handle(
        ModelLabFillReceiptsRequest(
            lanes=(ModelLabFillReceiptLane(lane="a", receipt="/r", engine="codex"),),
            window_s=120,
        )
    )
    assert result.receipts[0]["final"] is True
    assert clock.sleeps == [15]


def test_receipts_a_sonnet_lane_with_a_host_is_read_once() -> None:
    clock = FakeClock()
    reader = FakeReceipts({"/r": [{"status": "running", "host": "h1"}]})
    HandlerLabFillReceipts(reader=reader, clock=clock).handle(
        ModelLabFillReceiptsRequest(
            lanes=(ModelLabFillReceiptLane(lane="a", receipt="/r", engine="sonnet"),),
            window_s=120,
        )
    )
    assert reader.reads == 1
    assert clock.sleeps == []


def test_receipts_that_never_appear_are_found_false_at_the_window_not_forever() -> None:
    clock = FakeClock()
    result = HandlerLabFillReceipts(reader=FakeReceipts({}), clock=clock).handle(
        ModelLabFillReceiptsRequest(
            lanes=(ModelLabFillReceiptLane(lane="a", receipt="/missing"),), window_s=40
        )
    )
    assert result.receipts == ({"lane": "a", "found": False},)
    assert clock.t - START == 40


def test_receipts_never_read_past_the_absolute_end() -> None:
    clock = FakeClock()
    HandlerLabFillReceipts(reader=FakeReceipts({}), clock=clock).handle(
        ModelLabFillReceiptsRequest(
            lanes=(ModelLabFillReceiptLane(lane="a", receipt="/m"),),
            window_s=3000,
            end_epoch_s=int(START) + 20,
        )
    )
    assert clock.t - START == 20


def test_receipts_an_unreadable_receipt_is_not_found() -> None:
    class Raising:
        def read(self, path: str) -> Mapping[str, object] | None:
            raise LabFillPortError("EIO")

    result = HandlerLabFillReceipts(reader=Raising(), clock=FakeClock()).handle(
        ModelLabFillReceiptsRequest(
            lanes=(ModelLabFillReceiptLane(lane="a", receipt="/m"),), window_s=0
        )
    )
    assert result.receipts == ({"lane": "a", "found": False},)


def test_fallback_host_is_empty_when_every_host_is_limited_or_the_markers_are_unreadable() -> (
    None
):
    request = ModelLabFillFallbackHostRequest(ranked_hosts=("h1", "h2"))
    assert (
        HandlerLabFillFallbackHost(FakePlacement(limited=frozenset({"h1", "h2"})))
        .handle(request)
        .host
        == ""
    )
    assert (
        HandlerLabFillFallbackHost(FakePlacement(fail="boom")).handle(request).host
        == ""
    )


def _status_request(
    *,
    run_key: str = "2026-10-09T0100Z",
    friction_cells: str = "",
    idle: Mapping[str, object] | None = None,
    result_path: str = "",
) -> ModelLabFillStatusWriteRequest:
    return ModelLabFillStatusWriteRequest(
        run_key=run_key,
        cells=("STATUS", "lane=lab-fill", "lanes=none"),
        friction_cells=friction_cells,
        idle=idle if idle is not None else {"idle_alarm": False},
        result_path=result_path,
    )


def _status(
    appender: ProtocolLabFillStatusAppender | None,
    *,
    writer: ProtocolLabFillResultWriter | None = None,
) -> HandlerLabFillStatus:
    return HandlerLabFillStatus(
        appender=appender,
        writer=writer if writer is not None else FakeWriter(),
        clock=FakeClock(),
        host_name="mac",
    )


def test_status_with_no_appender_wired_answers_error_never_silence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ONEX_LAB_FILL_LEDGER_BUS_LANE", raising=False)
    result = asyncio.run(_status(None).handle(_status_request()))
    assert result.outcome == "error"
    assert "ONEX_LAB_FILL_LEDGER_BUS_LANE" in result.message


def test_status_a_refused_friction_row_stops_before_status_and_the_result_says_not_recorded() -> (
    None
):
    appender = FakeAppender(statuses=[EnumWorkLedgerAppendStatus.REFUSED])
    writer = FakeWriter()
    result = asyncio.run(
        _status(appender, writer=writer).handle(
            _status_request(
                friction_cells="FRICTION | lane=lab-fill",
                result_path="/r.json",
                idle={"idle_alarm": True},
            )
        )
    )
    assert result.outcome == "friction-refused"
    assert not result.friction_recorded
    assert len(appender.sent) == 1
    assert writer.written["/r.json"] == {"idle_alarm": True, "friction_recorded": False}


def test_status_a_resent_row_answers_duplicate() -> None:
    appender = FakeAppender(statuses=[EnumWorkLedgerAppendStatus.DUPLICATE])
    result = asyncio.run(_status(appender).handle(_status_request()))
    assert result.outcome == "duplicate"


def test_status_the_same_fire_always_uses_the_same_request_id() -> None:
    first, second = FakeAppender(), FakeAppender()
    asyncio.run(_status(first).handle(_status_request()))
    asyncio.run(_status(second).handle(_status_request()))
    assert first.sent[0].request_id == second.sent[0].request_id


def test_status_no_receipt_in_time_is_an_error_that_says_to_resend() -> None:
    result = asyncio.run(
        _status(FakeAppender(statuses=[None])).handle(_status_request())
    )
    assert result.outcome == "error"
    assert "resend under the same request id" in result.message


def test_status_a_bus_that_cannot_be_opened_is_an_error_with_its_cause() -> None:
    result = asyncio.run(
        _status(FakeAppender(raises=RuntimeError("no broker"))).handle(
            _status_request()
        )
    )
    assert result.outcome == "error"
    assert "RuntimeError: no broker" in result.message


def test_status_result_file_failure_is_reported_not_raised() -> None:
    result = asyncio.run(
        _status(FakeAppender(), writer=FakeWriter(fail=True)).handle(
            _status_request(result_path="/r.json")
        )
    )
    assert result.outcome == "appended"
    assert result.result_written is False


def test_status_a_ledger_refusal_is_named() -> None:
    result = asyncio.run(
        _status(FakeAppender(statuses=[EnumWorkLedgerAppendStatus.REFUSED])).handle(
            _status_request()
        )
    )
    assert result.outcome == "refused"


def test_launch_a_nonzero_exit_is_not_a_launch_even_when_it_printed_detached(
    tmp_path: Path,
) -> None:
    outcome = LabFillCommandOutcome(1, "DETACHED receipt=/r.json\nthen it failed\n", "")
    result = _launcher(runner=FakeRunner(outcome=outcome)).handle(
        _launch_request(tmp_path)
    )
    assert (result.detached, result.receipt) == (False, "")


def test_status_different_fires_never_share_a_request_id() -> None:
    first, second = FakeAppender(), FakeAppender()
    asyncio.run(_status(first).handle(_status_request()))
    asyncio.run(_status(second).handle(_status_request(run_key="2026-10-09T0120Z")))
    assert first.sent[0].request_id != second.sent[0].request_id
