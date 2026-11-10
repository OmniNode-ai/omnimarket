# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Parent notice recovery (OMN-23389): a failed placement reads failed, and each notice is consumed once."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_lane_parent_notice_recovery_effect.handlers.adapters import (
    ArgvRecoveryDispatcher,
    InMemoryLedgerLines,
    InMemoryNoticeSink,
    InMemoryNoticeStateStore,
    InMemoryReceipts,
    InMemoryRecoveryDispatcher,
    JsonFileNoticeStateStore,
)
from omnimarket.nodes.node_lane_parent_notice_recovery_effect.handlers.handler_notice_recovery import (
    HandlerNoticeRecovery,
    NoticeRefusalError,
    family_root,
    load_overlay,
    read_lane_states,
)
from omnimarket.nodes.node_lane_parent_notice_recovery_effect.models.model_notice_recovery import (
    EnumLaneReadState,
    EnumNoticeAction,
    ModelNoticeConsumeRequest,
    ModelNoticeConsumeResult,
    ModelNoticeRecoveryOverlay,
)
from omnimarket.nodes.node_lane_parent_notice_recovery_effect.node import (
    NodeLaneParentNoticeRecoveryEffect,
)
from omnimarket.nodes.node_lane_parent_notice_recovery_effect.runtime_notice_recovery import (
    main,
)

T0 = datetime(2026, 11, 9, 20, 28, 58, tzinfo=UTC)
FAILURES = ("node-unavailable",)
OVERLAY = ModelNoticeRecoveryOverlay(
    parent_lane="parent-lane",
    failure_statuses=FAILURES,
    retry_budget=3,
    backoff_seconds=(120, 600),
    state_file="$HOME/state.json",
    recovery_argv=(
        "start",
        "{lane}",
        "{brief_file}",
        "{ticket}",
        "{parent}",
        "{model}",
        "{original}",
        "{ledger}",
    ),
    recovery_timeout_seconds=30,
    ack_argv=("ack", "{lane}", "{notice}", "{action}"),
    escalate_argv=("escalate", "{row}"),
)


def stamp(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def claim(lane: str, at: datetime = T0 - timedelta(minutes=1)) -> str:
    return f"{stamp(at)} | CLAIM | lane={lane} | ticket=OMN-1 | parent=parent-lane | host=h1"


def notice(lane: str, status: str = "node-unavailable", at: datetime = T0) -> str:
    return (
        f"{stamp(at)} | STATUS | lane={lane} | ticket=OMN-1 | to=parent-lane | status={status} | host=none | exit=75 "
        f"| run=rlane-{lane}-1 | receipt=/r/{lane}.json | detached lane {lane} ended status={status}"
    )


def receipt(lane: str) -> dict[str, dict[str, object]]:
    return {
        f"/r/{lane}.json": {
            "brief": f"/b/{lane}.md",
            "model": "sonnet",
            "ticket": "OMN-1",
        }
    }


class Rig:
    def __init__(
        self,
        lines: list[str],
        receipts: dict[str, dict[str, object]] | None = None,
        refuse: bool = False,
    ) -> None:
        self.ledger = InMemoryLedgerLines(lines)
        self.state = InMemoryNoticeStateStore()
        self.dispatcher = InMemoryRecoveryDispatcher(refuse=refuse)
        self.sink = InMemoryNoticeSink()
        self.receipts = InMemoryReceipts(
            receipts if receipts is not None else receipt("ovbroker")
        )
        self.handler = HandlerNoticeRecovery(
            ledger=self.ledger,
            store=self.state,
            receipts=self.receipts,
            dispatcher=self.dispatcher,
            sink=self.sink,
            overlay=OVERLAY,
        )

    def fire(self, after: int = 0) -> ModelNoticeConsumeResult:
        return self.handler.handle(
            ModelNoticeConsumeRequest(now=stamp(T0 + timedelta(seconds=after)))
        )


def test_control_a_node_unavailable_lane_reads_running_without_the_failure_status() -> (
    None
):
    """The failing control: the lane read with no failure status declared reads the failed placement as running."""
    lines = [claim("ovbroker"), notice("ovbroker")]
    [lane] = read_lane_states(lines, ())
    assert lane.state is EnumLaneReadState.RUNNING


def test_a_node_unavailable_lane_reads_failed() -> None:
    """AC3 falsifier: a node-unavailable fixture that reads running."""
    [lane] = read_lane_states([claim("ovbroker"), notice("ovbroker")], FAILURES)
    assert lane.state is EnumLaneReadState.FAILED
    assert "node-unavailable" in lane.reason


def test_the_lane_read_keeps_a_running_lane_running_and_a_terminal_ended() -> None:
    lines = [
        claim("alive"),
        claim("done"),
        f"{stamp(T0)} | TERMINAL | lane=done | outcome=handed-off | ticket=OMN-1",
        claim("late"),
        notice("late"),
        f"{stamp(T0 + timedelta(seconds=5))} | TERMINAL | lane=late | outcome=failed | ticket=OMN-1",
    ]
    states = {lane.lane: lane for lane in read_lane_states(lines, FAILURES)}
    assert states["alive"].state is EnumLaneReadState.RUNNING
    assert states["done"].state is EnumLaneReadState.ENDED
    assert states["done"].reason == "handed-off"
    assert states["late"].state is EnumLaneReadState.ENDED


def test_golden_chain_a_failed_placement_is_recovered_once_and_acknowledged() -> None:
    rig = Rig([claim("ovbroker"), notice("ovbroker")])
    first = rig.fire()
    assert [o.action for o in first.outcomes] == [EnumNoticeAction.RECOVERED]
    assert first.outcomes[0].recovery_lane == "ovbroker-r1"
    assert rig.dispatcher.lanes == [("ovbroker-r1", "ovbroker", "/b/ovbroker.md")]
    assert rig.sink.acks == [("ovbroker", stamp(T0), "recovered")]
    assert first.cursor_ts == stamp(T0)
    assert first.lanes[0].state is EnumLaneReadState.FAILED
    # The next fire reads the same ledger and starts nothing.
    second = rig.fire(after=60)
    assert second.outcomes == ()
    assert len(rig.dispatcher.lanes) == 1


def test_a_restart_between_the_dispatch_and_the_cursor_starts_no_second_lane(
    tmp_path: Path,
) -> None:
    """Drill: the first pass dies after the start is recorded and before the cursor moves."""
    state_file = tmp_path / "state.json"
    ledger = InMemoryLedgerLines([claim("ovbroker"), notice("ovbroker")])
    dispatcher = InMemoryRecoveryDispatcher()

    class DiesOnAck(InMemoryNoticeSink):
        def acknowledge(self, lane: str, notice: str, action: str) -> None:
            raise KeyboardInterrupt

    def handler(sink: InMemoryNoticeSink) -> HandlerNoticeRecovery:
        return HandlerNoticeRecovery(
            ledger=ledger,
            store=JsonFileNoticeStateStore(state_file),
            receipts=InMemoryReceipts(receipt("ovbroker")),
            dispatcher=dispatcher,
            sink=sink,
            overlay=OVERLAY,
        )

    with pytest.raises(KeyboardInterrupt):
        handler(DiesOnAck()).handle(ModelNoticeConsumeRequest(now=stamp(T0)))
    assert len(dispatcher.lanes) == 1
    assert JsonFileNoticeStateStore(state_file).load().cursor_ts == ""
    survivor = InMemoryNoticeSink()
    result = handler(survivor).handle(
        ModelNoticeConsumeRequest(now=stamp(T0 + timedelta(seconds=30)))
    )
    assert [o.action for o in result.outcomes] == [EnumNoticeAction.ALREADY_RECOVERED]
    assert len(dispatcher.lanes) == 1
    assert survivor.acks == [("ovbroker", stamp(T0), "already-recovered")]
    assert JsonFileNoticeStateStore(state_file).load().cursor_ts == stamp(T0)


def test_a_notice_that_is_not_a_failure_is_acknowledged_and_starts_nothing() -> None:
    rig = Rig([claim("worker"), notice("worker", status="done")])
    result = rig.fire()
    assert [o.action for o in result.outcomes] == [EnumNoticeAction.ACKNOWLEDGED]
    assert rig.dispatcher.lanes == []
    assert len(rig.sink.acks) == 1


def test_notices_to_another_parent_are_not_consumed() -> None:
    other = notice("ovbroker").replace("to=parent-lane", "to=someone-else")
    rig = Rig([claim("ovbroker"), other])
    assert rig.fire().outcomes == ()


def test_two_notices_at_one_second_are_both_consumed_once() -> None:
    rig = Rig(
        [claim("a"), claim("b"), notice("a"), notice("b")],
        {**receipt("a"), **receipt("b")},
    )
    assert len(rig.fire().outcomes) == 2
    assert rig.fire(after=10).outcomes == ()
    assert sorted(lane for lane, _, _ in rig.dispatcher.lanes) == ["a-r1", "b-r1"]


def test_a_persistently_unavailable_placement_ends_in_one_escalation_not_an_endless_chain() -> (
    None
):
    """Drill: every recovery lane fails placement again; the budget of 3 stops the chain with one row."""
    lines = [claim("ovbroker"), notice("ovbroker", at=T0)]
    rig = Rig(
        lines,
        {
            **receipt("ovbroker"),
            **receipt("ovbroker-r1"),
            **receipt("ovbroker-r2"),
            **receipt("ovbroker-r3"),
        },
    )
    rig.fire(after=0)
    for n, offset in ((1, 200), (2, 1000), (3, 2000)):
        lines.append(notice(f"ovbroker-r{n}", at=T0 + timedelta(seconds=offset - 10)))
        rig.fire(after=offset + 700)
    result = rig.fire(after=4000)
    started = [lane for lane, _, _ in rig.dispatcher.lanes]
    assert started == ["ovbroker-r1", "ovbroker-r2", "ovbroker-r3"]
    assert len(rig.sink.escalations) == 1
    assert "ovbroker" in rig.sink.escalations[0]
    assert result.outcomes == ()


def test_a_second_failure_inside_the_backoff_waits_and_holds_the_cursor() -> None:
    lines = [claim("ovbroker"), notice("ovbroker")]
    rig = Rig(lines, {**receipt("ovbroker"), **receipt("ovbroker-r1")})
    rig.fire()
    lines.append(notice("ovbroker-r1", at=T0 + timedelta(seconds=30)))
    held = rig.fire(after=60)
    assert held.stopped is True
    assert held.outcomes[0].action is EnumNoticeAction.DEFERRED
    assert held.cursor_ts == stamp(T0)
    assert len(rig.dispatcher.lanes) == 1
    later = rig.fire(after=200)
    assert [o.action for o in later.outcomes] == [EnumNoticeAction.RECOVERED]
    assert later.outcomes[0].recovery_lane == "ovbroker-r2"


def test_error_chain_a_dispatch_that_does_not_start_holds_the_cursor_and_retries_the_same_lane() -> (
    None
):
    rig = Rig([claim("ovbroker"), notice("ovbroker")], refuse=True)
    first = rig.fire()
    assert first.stopped is True
    assert first.outcomes[0].action is EnumNoticeAction.DISPATCH_FAILED
    assert first.cursor_ts == ""
    assert rig.sink.acks == []
    rig.dispatcher.refuse = False
    second = rig.fire(after=10)
    assert [o.action for o in second.outcomes] == [EnumNoticeAction.RECOVERED]
    assert second.outcomes[0].recovery_lane == "ovbroker-r1"


def test_error_chain_a_receipt_that_cannot_be_read_escalates_instead_of_guessing_a_brief() -> (
    None
):
    rig = Rig([claim("ovbroker"), notice("ovbroker")], receipts={})
    result = rig.fire()
    assert result.outcomes[0].action is EnumNoticeAction.ESCALATED
    assert rig.dispatcher.lanes == []
    assert len(rig.sink.escalations) == 1


def test_family_root_strips_the_recovery_suffix_only() -> None:
    assert family_root("ovbroker-r12") == "ovbroker"
    assert family_root("fill-r2d2") == "fill-r2d2"


class _Recorder:
    def __init__(self, code: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.code = code

    def __call__(
        self, argv: Sequence[str], timeout: int
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(list(argv), self.code, "", "")


def test_the_argv_dispatcher_fills_every_placeholder() -> None:
    runner = _Recorder()
    ArgvRecoveryDispatcher(OVERLAY.recovery_argv, 5, runner).dispatch(
        "a-r1", "a", "/b/a.md", "OMN-1", "parent-lane", "sonnet", "/l/ledger.md"
    )
    assert runner.calls == [
        [
            "start",
            "a-r1",
            "/b/a.md",
            "OMN-1",
            "parent-lane",
            "sonnet",
            "a",
            "/l/ledger.md",
        ]
    ]


def test_node_entry_and_the_command_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    node = NodeLaneParentNoticeRecoveryEffect()
    node.handler = HandlerNoticeRecovery(
        ledger=InMemoryLedgerLines([claim("a")]), overlay=OVERLAY
    )
    assert node.handle(ModelNoticeConsumeRequest(now=stamp(T0))).outcomes == ()
    ledger = tmp_path / "ledger.md"
    ledger.write_text("\n".join([claim("ovbroker"), notice("ovbroker")]) + "\n")
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(yaml.safe_dump(OVERLAY.model_dump(mode="json")))
    assert main(["lanes", "--ledger", str(ledger), "--overlay", str(overlay)]) == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith("ovbroker failed placement node-unavailable")


def test_error_chain_an_unreadable_ledger_or_overlay_is_indeterminate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(yaml.safe_dump(OVERLAY.model_dump(mode="json")))
    assert (
        main(
            [
                "consume",
                "--ledger",
                str(tmp_path / "missing.md"),
                "--overlay",
                str(overlay),
            ]
        )
        == 3
    )
    assert "INDETERMINATE" in capsys.readouterr().err
    with pytest.raises(NoticeRefusalError):
        load_overlay(str(tmp_path / "missing.yaml"))


def test_the_shipped_overlays_load() -> None:
    root = (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_lane_parent_notice_recovery_effect"
    )
    for name in ("overlay.yaml", "overlay.example.yaml"):
        overlay = load_overlay(str(root / name))
        assert overlay.retry_budget >= 1
    assert json.loads(
        json.dumps(load_overlay(str(root / "overlay.yaml")).failure_statuses)
    ) == ["node-unavailable"]
