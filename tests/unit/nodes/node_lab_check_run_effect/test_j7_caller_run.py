# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""J7 runner (OMN-23420): golden and error chain event tests and the failing controls, judged by the floor's own fold.

Before this run the J7 join (piece 7, the process spine, to piece 8, the central test harness) had a join test and nothing
that ran it: the floor read J7 ``not_run``. The runner answers each piece 7 golden reading (a stop drill STATUS row) with a send
past a stop whose lab check posts on a test head, and writes piece 8's chain under the reading's correlation id; the join refusal
node answers each piece 7 failure. The golden case reads ``golden_only`` and, with the refusal, ``passed``. The superseded-attempt
drill is piece 8's own error chain.

Failing controls: the run removed leaves the join broken (a piece 7 failure nobody answers); a stop gate that ignores the stop
leaves the join not golden with the typed failure on the journal; a check-run API that accepts an invalidation and applies nothing
(the named break ``deaf-invalidation``) writes no error receipt; a planted rename of the stop drill's verdict cell finds nothing owed.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from omnibase_core.models.contracts.subcontracts.model_fsm_subcontract import (
    ModelFSMSubcontract,
)
from pydantic import ValidationError

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.handlers.rules_stop_gate import stop_gate
from omnimarket.models.floor_17a import (
    ModelFloorReceiptInput,
    ModelJoinDef,
    ModelNodeEmission,
    ModelPieceDef,
    ModelPieceEventRule,
)
from omnimarket.models.model_lab_check_run_join_j7_run import (
    EnumJoinJ7RunOutcome,
    ModelJoinJ7RunCommand,
    ModelJoinJ7RunItem,
)
from omnimarket.models.stop_gate import (
    EnumStopMode,
    ModelStopGateRequest,
    ModelStopGateResult,
)
from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
    HandlerFloorReceipt,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.ledger_reads import (
    piece_events,
)
from omnimarket.nodes.node_join_refusal_effect import HandlerJoinRefusal
from omnimarket.nodes.node_join_refusal_effect.models import ModelJoinRefusalCommand
from omnimarket.nodes.node_join_refusal_effect.overlay import ModelJoinRefusalOverlay
from omnimarket.nodes.node_lab_check_run_effect import runtime_join_j7
from omnimarket.nodes.node_lab_check_run_effect.handlers.handler_lab_check_run_join_j7_run import (
    HandlerLabCheckRunJoinJ7Run,
    JoinJ7Rig,
)
from omnimarket.nodes.node_lab_check_run_effect.overlay_join_j7 import (
    ModelJoinJ7Overlay,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
    evidence_row_from_text,
)

pytestmark = pytest.mark.unit

EXAMPLE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_lab_check_run_effect/overlay_join_j7.example.yaml"
)
NOW = datetime(2026, 11, 9, 12, 0, 0, tzinfo=UTC)
LANE = "example-stop-drill"
GOLDEN_AT = NOW - timedelta(minutes=30)
ERROR_AT = NOW - timedelta(minutes=45)
BASE = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
OVERLAY = ModelJoinJ7Overlay.model_validate(
    {**BASE, "max_chains_per_run": 1, "allow_injected_break": True}
)
FROM_PIECE = 7
TO_PIECE = 8
JOIN_ID = OVERLAY.join_id
RULE = ModelPieceEventRule.model_validate(
    {
        "piece": FROM_PIECE,
        "node": "example_stop_drill_node:run",
        "golden": {
            "row_type": "STATUS",
            "lane": LANE,
            "cells": {"src": LANE, "drill": "no", "verdict": "WORKING"},
        },
        "error": {
            "row_type": "STATUS",
            "lane": LANE,
            "cells": {"src": LANE, "verdict": "NOT_WORKING"},
        },
        "failure_code_cell": "reason",
        "failure_default": "stop_drill_untyped",
        "error_drill_cell": "drill",
        "lookback_days": 7,
    }
)


class _Clock:
    def __init__(self, start: datetime) -> None:
        self._t = start

    def __call__(self) -> datetime:
        self._t += timedelta(seconds=1)
        return self._t


def _reading_row(
    at: datetime, verdict: str, *, drill: str = "no", verdict_cell: str = "verdict"
) -> str:
    stamp = at.strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f"{stamp} | STATUS | lane={LANE} | src={LANE} | {verdict_cell}={verdict}"
        f" | reason=example_failure | drill={drill} | probed_at={stamp}"
    )


class _Rig:
    """Piece 7's rows on the ledger source, the journal in memory, the twin the in-memory gate store and check-run API."""

    def __init__(self, rows: list[str]) -> None:
        self.source = InMemoryLedger(rows)
        self.sink = InMemoryEmissionSink()
        self.clock = _Clock(NOW)

    def runner(
        self,
        *,
        golden: JoinJ7Rig | None = None,
        drill: JoinJ7Rig | None = None,
        deaf: bool = False,
        overlay: ModelJoinJ7Overlay = OVERLAY,
        clock: Callable[[], datetime] | None = None,
    ) -> HandlerLabCheckRunJoinJ7Run:
        return HandlerLabCheckRunJoinJ7Run(
            source=self.source,
            sink=self.sink,
            upstream=RULE,
            overlay=overlay,
            golden_rig=lambda: golden or runtime_join_j7.build_rig(overlay),
            drill_rig=lambda: drill or runtime_join_j7.build_rig(overlay, deaf=deaf),
            window_hours=48,
            to_piece=TO_PIECE,
            backend="in_memory",
            clock=clock or self.clock,
        )

    def piece8(self) -> list[ModelNodeEmission]:
        return [e for e in self.sink.events if e.piece == TO_PIECE]


def _fold(emissions: list[ModelNodeEmission], now: datetime) -> tuple[str, list[str]]:
    receipts = HandlerFloorReceipt().handle(
        ModelFloorReceiptInput(
            now=now,
            emissions=tuple(emissions),
            pieces=tuple(
                ModelPieceDef(piece=p, name=f"piece {p}", reading_max_age_s=86400)
                for p in (FROM_PIECE, TO_PIECE)
            ),
            joins=(
                ModelJoinDef(
                    id=JOIN_ID,
                    from_piece=FROM_PIECE,
                    to_piece=TO_PIECE,
                    label="process spine to CI",
                ),
            ),
            freshness_max_age_s=86400,
            error_max_age_s=86400,
            chain_deadline_s=900,
        )
    )
    join = next(j for j in receipts.joins if j.join == JOIN_ID)
    return join.state, [f.code for f in receipts.findings]


def _floor_read(
    rig: _Rig, now: datetime, *, control: bool = True
) -> tuple[str, list[str]]:
    """What the floor folds: piece 7's chains from the ledger rows, plus every emission on the journal.

    ``control=False`` leaves the failing control's chains out, which is the journal as it stood before the control existed.
    """
    rows = [r for line in rig.source.lines if (r := evidence_row_from_text(line))]
    journal = rig.sink.events if control else _without_control(rig.sink.events)
    return _fold([*piece_events(rows, (RULE,)), *journal], now)


class _JumpClock:
    """A clock the test can jump past an overlay window in one step."""

    def __init__(self, start: datetime) -> None:
        self._t = start

    def __call__(self) -> datetime:
        return self._t

    def advance(self, by: timedelta) -> None:
        self._t += by


def _without_control(events: list[ModelNodeEmission]) -> list[ModelNodeEmission]:
    """The journal with the control's own chains out: nothing under its correlation id or its piece 7 node."""
    prefix = OVERLAY.control.id_prefix
    node = OVERLAY.control.upstream_node
    return [
        e for e in events if not e.correlation_id.startswith(prefix) and e.node != node
    ]


def _refusal_overlay(downstream_node: str) -> dict[str, object]:
    return {
        "ledger_read": {
            "route": "file",
            "path_env": "J7_TEST_LEDGER",
            "archive_subdir": "archive",
            "timeout_seconds": 60,
            "row_limit": 5000,
        },
        "emission": {
            "home_env": "J7_TEST_HOME",
            "journal_relpath": "emissions.jsonl",
            "window_hours": 48,
        },
        "joins": [
            {
                "join": JOIN_ID,
                "from_piece": FROM_PIECE,
                "to_piece": TO_PIECE,
                "downstream_node": downstream_node,
                "injected_fault": "Drop the piece 8 refusal from the emission journal",
                "upstream": RULE.model_dump(mode="json"),
            }
        ],
    }


def _refuse(rig: _Rig, now: datetime) -> None:
    """The join refusal node, as the publish cycle runs it, over the same rows and the same journal."""
    HandlerJoinRefusal(
        overlay=ModelJoinRefusalOverlay.model_validate(
            _refusal_overlay(OVERLAY.emission_node)
        ),
        source=rig.source,
        sink=rig.sink,
        backend="in_memory",
        clock=lambda: now,
    ).handle(ModelJoinRefusalCommand(write=True))


# --- the golden chain ------------------------------------------------------------------------------------------------


def test_golden_run_posts_the_check_past_the_stop_and_the_floor_reads_golden_only() -> (
    None
):
    rig = _Rig([_reading_row(GOLDEN_AT, "WORKING")])

    result = rig.runner().handle(ModelJoinJ7RunCommand())

    assert result.read_ok
    assert (result.owed, result.answered) == (1, 0)
    landed = [i for i in result.items if not i.drill and not i.control]
    assert [i.outcome for i in landed] == [EnumJoinJ7RunOutcome.LANDED]
    chain = [e for e in rig.piece8() if e.correlation_id == landed[0].correlation_id]
    assert [e.phase for e in chain] == ["start", "success"]
    assert chain[-1].counters == {
        "sends_refused": 1,
        "sends_admitted": 1,
        "checks_posted": 1,
        "other_lane_refused": 1,
    }
    assert all(e.backend == "in_memory" and not e.drill for e in chain)
    assert _floor_read(rig, NOW + timedelta(minutes=5), control=False) == (
        "golden_only",
        [],
    )
    # With the failing control's error side beside the golden chain the join passes.
    assert _floor_read(rig, NOW + timedelta(minutes=5)) == ("passed", [])


def test_golden_and_error_together_read_passed() -> None:
    rig = _Rig(
        [_reading_row(GOLDEN_AT, "WORKING"), _reading_row(ERROR_AT, "NOT_WORKING")]
    )

    rig.runner().handle(ModelJoinJ7RunCommand())
    _refuse(rig, NOW + timedelta(minutes=2))

    assert _floor_read(rig, NOW + timedelta(minutes=5)) == ("passed", [])


def test_golden_a_rerun_posts_nothing_for_a_chain_piece_8_already_answered() -> None:
    rig = _Rig([_reading_row(GOLDEN_AT, "WORKING")])
    runner = rig.runner()
    runner.handle(ModelJoinJ7RunCommand())
    written = len(rig.sink.events)

    again = runner.handle(ModelJoinJ7RunCommand())

    assert (again.owed, again.answered, again.drill_owed, again.items) == (
        0,
        1,
        False,
        (),
    )
    assert len(rig.sink.events) == written


def test_golden_a_run_answers_at_most_the_overlay_bound_newest_first() -> None:
    older = GOLDEN_AT - timedelta(hours=2)
    rig = _Rig([_reading_row(older, "WORKING"), _reading_row(GOLDEN_AT, "WORKING")])

    result = rig.runner().handle(ModelJoinJ7RunCommand())

    chains = [i for i in result.items if not i.drill and not i.control]
    assert (result.owed, len(chains)) == (2, 1)
    newest_row = evidence_row_from_text(rig.source.lines[1])
    assert newest_row is not None
    assert (
        chains[0].correlation_id
        == piece_events([newest_row], (RULE,))[0].correlation_id
    )


# --- the error chain: the superseded-attempt drill --------------------------------------------------------------------


def test_error_the_drill_writes_piece_8s_own_error_chain_once_per_window() -> None:
    rig = _Rig([])
    runner = rig.runner()

    first = runner.handle(ModelJoinJ7RunCommand())
    second = runner.handle(ModelJoinJ7RunCommand())

    drill_items = [i for i in first.items if i.drill]
    assert [i.outcome for i in drill_items] == [EnumJoinJ7RunOutcome.DRILL_RECORDED]
    assert second.drill_owed is False
    assert second.items == ()
    chain = _without_control(rig.piece8())
    assert [(e.phase, e.drill) for e in chain] == [("start", True), ("failure", True)]
    assert chain[-1].failure_code == "superseded_attempt_refused"
    assert len(chain) == 2
    # A drill chain is piece 8's error receipt; it is no evidence for the join.
    assert _floor_read(rig, NOW + timedelta(minutes=5), control=False) == (
        "not_run",
        [],
    )


def test_error_a_failed_or_drill_reading_is_never_answered_by_the_runner() -> None:
    rig = _Rig(
        [
            _reading_row(ERROR_AT, "NOT_WORKING"),
            _reading_row(ERROR_AT, "NOT_WORKING", drill="yes"),
        ]
    )

    result = rig.runner().handle(ModelJoinJ7RunCommand())

    assert result.owed == 0
    assert [i.drill for i in result.items if not i.control] == [True]


def test_error_an_unreadable_ledger_is_not_an_empty_answer() -> None:
    rig = _Rig([_reading_row(GOLDEN_AT, "WORKING")])
    rig.source.read_error = "ledger-file-unreadable:OSError"

    result = rig.runner().handle(ModelJoinJ7RunCommand())

    assert result.read_ok is False
    assert "unread" in result.reason
    assert rig.sink.events == []


def test_error_a_head_that_already_reads_pass_is_a_typed_failure() -> None:
    from omnimarket.nodes.node_lab_check_run_effect.models.model_lab_check_run import (
        EnumLabCheckRunConclusion,
        EnumLabCheckRunOperation,
        ModelLabCheckRunRequest,
    )

    twin = runtime_join_j7.build_rig(OVERLAY)
    twin.labcheck.handle(
        ModelLabCheckRunRequest(
            correlation_id="seed",
            operation=EnumLabCheckRunOperation.POST,
            repository=OVERLAY.repository,
            head_sha=OVERLAY.head_sha,
            current_attempt_id=OVERLAY.current_attempt_id,
            dispatched_at=NOW,
            now=NOW,
            attempt_id=OVERLAY.current_attempt_id,
            conclusion=EnumLabCheckRunConclusion.SUCCESS,
        )
    )
    rig = _Rig([_reading_row(GOLDEN_AT, "WORKING")])

    result = rig.runner(golden=twin).handle(ModelJoinJ7RunCommand())

    chains = [i for i in result.items if not i.drill and not i.control]
    assert [i.outcome for i in chains] == [EnumJoinJ7RunOutcome.HEAD_NOT_PASSING]
    assert chains[0].failure_code == "head_already_passing"
    assert [e.phase for e in _without_control(rig.piece8()) if not e.drill] == [
        "start",
        "failure",
    ]


def test_a_probe_reads_what_is_owed_and_writes_nothing() -> None:
    rig = _Rig([_reading_row(GOLDEN_AT, "WORKING")])

    result = rig.runner().handle(ModelJoinJ7RunCommand(probe=True))

    assert (result.owed, result.drill_owed, result.items) == (1, True, ())
    assert rig.sink.events == []


# --- the failing controls ----------------------------------------------------------------------------------------------


def test_control_without_the_run_and_the_refusal_the_join_reads_broken() -> None:
    """The reading a piece 7 failure gets with piece 8 silent past the deadline."""
    rig = _Rig([_reading_row(ERROR_AT, "NOT_WORKING")])

    state, findings = _floor_read(rig, NOW + timedelta(hours=1))

    assert state == "broken"
    assert findings == ["downstream_missing_typed_failure"]


def test_control_a_stop_gate_that_ignores_the_stop_leaves_the_join_not_golden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnimarket.nodes.node_lab_check_run_effect.handlers import (
        handler_lab_check_run_join_j7_run as module,
    )

    def ignoring(
        request: ModelStopGateRequest, fsm: ModelFSMSubcontract
    ) -> ModelStopGateResult:
        return stop_gate(request.model_copy(update={"stops": ()}), fsm)

    monkeypatch.setattr(module, "stop_gate", ignoring)
    rig = _Rig([_reading_row(GOLDEN_AT, "WORKING")])

    result = rig.runner().handle(ModelJoinJ7RunCommand())

    chains = [i for i in result.items if not i.drill and not i.control]
    assert [i.outcome for i in chains] == [EnumJoinJ7RunOutcome.CAUSE_LANE_NOT_ADMITTED]
    assert result.ok is False
    chain = [
        e
        for e in _without_control(rig.piece8())
        if e.correlation_id == chains[0].correlation_id
    ]
    assert [e.phase for e in chain] == ["start", "failure"]
    assert _floor_read(rig, NOW + timedelta(minutes=5))[0] == "not_run"


def test_control_the_named_break_deaf_invalidation_writes_no_error_receipt() -> None:
    rig = _Rig([])

    result = rig.runner(deaf=True).handle(ModelJoinJ7RunCommand())

    drills = [i for i in result.items if i.drill]
    assert [i.outcome for i in drills] == [EnumJoinJ7RunOutcome.DRILL_NOT_HELD]
    assert result.ok is False
    assert all(e.correlation_id != drills[0].correlation_id for e in rig.sink.events)


def test_control_a_planted_rename_of_the_verdict_cell_finds_nothing_owed() -> None:
    rig = _Rig([_reading_row(GOLDEN_AT, "WORKING", verdict_cell="outcome_verdict")])

    result = rig.runner().handle(ModelJoinJ7RunCommand())

    assert result.owed == 0
    assert [i for i in result.items if not i.drill and not i.control] == []
    assert [i for i in result.items if i.control] == [
        ModelJoinJ7RunItem(
            correlation_id=result.items[0].correlation_id,
            outcome=EnumJoinJ7RunOutcome.CONTROL_RECORDED,
            control=True,
            failure_code=OVERLAY.control.failure_code,
            detail="the stop gate admitted a lane the stop on "
            f"{OVERLAY.stopped_piece} must refuse",
        )
    ]


def test_control_a_cause_lane_and_another_lane_are_two_lanes() -> None:
    lanes = HandlerLabCheckRunJoinJ7Run(
        source=InMemoryLedger([]),
        sink=InMemoryEmissionSink(),
        upstream=RULE,
        overlay=OVERLAY,
        golden_rig=lambda: runtime_join_j7.build_rig(OVERLAY),
        drill_rig=lambda: runtime_join_j7.build_rig(OVERLAY),
        window_hours=48,
        to_piece=TO_PIECE,
    )._lanes()

    assert lanes[0] != lanes[1]


def test_error_the_failing_control_gives_the_join_its_error_side_from_a_non_drill_run() -> (
    None
):
    rig = _Rig([])

    result = rig.runner().handle(ModelJoinJ7RunCommand())

    control = next(i for i in result.items if i.control)
    assert control.outcome is EnumJoinJ7RunOutcome.CONTROL_RECORDED
    assert not control.drill
    piece7 = [
        e
        for e in rig.sink.events
        if e.piece == FROM_PIECE and e.correlation_id == control.correlation_id
    ]
    assert [e.phase for e in piece7] == ["start", "failure"]
    assert piece7[-1].failure_code == OVERLAY.control.failure_code
    assert all(not e.drill for e in piece7)
    piece8 = [
        e
        for e in rig.sink.events
        if e.piece == TO_PIECE and e.correlation_id == control.correlation_id
    ]
    assert [e.phase for e in piece8] == ["failure"]
    assert piece8[0].failure_code == f"upstream_{OVERLAY.control.failure_code}"
    assert _floor_read(rig, NOW + timedelta(minutes=5)) == ("error_only", [])


def test_golden_and_the_control_together_read_passed() -> None:
    rig = _Rig([_reading_row(GOLDEN_AT, "WORKING")])

    result = rig.runner().handle(ModelJoinJ7RunCommand())

    assert next(i for i in result.items if i.control).outcome is (
        EnumJoinJ7RunOutcome.CONTROL_RECORDED
    )
    assert _floor_read(rig, NOW + timedelta(minutes=5)) == ("passed", [])


def test_control_the_gate_that_still_refuses_in_the_control_mode_turns_the_run_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnimarket.nodes.node_lab_check_run_effect.handlers import (
        handler_lab_check_run_join_j7_run as module,
    )

    def enforcing(
        request: ModelStopGateRequest, fsm: ModelFSMSubcontract
    ) -> ModelStopGateResult:
        if request.policy.modes == {OVERLAY.stop_class: OVERLAY.control.mode}:
            return stop_gate(
                request.model_copy(
                    update={
                        "policy": request.policy.model_copy(
                            update={"modes": {OVERLAY.stop_class: EnumStopMode.ENFORCE}}
                        )
                    }
                ),
                fsm,
            )
        return stop_gate(request, fsm)

    monkeypatch.setattr(module, "stop_gate", enforcing)
    rig = _Rig([])

    result = rig.runner().handle(ModelJoinJ7RunCommand())

    control = next(i for i in result.items if i.control)
    assert control.outcome is EnumJoinJ7RunOutcome.CONTROL_NOT_BROKEN
    assert result.ok is False
    assert not any(e.correlation_id == control.correlation_id for e in rig.sink.events)
    assert _floor_read(rig, NOW + timedelta(minutes=5))[0] != "passed"


def test_control_is_written_once_per_window_and_owed_again_after_it() -> None:
    rig = _Rig([])
    jump = _JumpClock(NOW)
    runner = rig.runner(clock=jump)

    probe = runner.handle(ModelJoinJ7RunCommand(probe=True))
    assert probe.control_owed is True
    assert probe.items == ()
    assert rig.sink.events == []

    first = runner.handle(ModelJoinJ7RunCommand())
    assert first.control_owed is True
    control = next(i for i in first.items if i.control)
    assert control.outcome is EnumJoinJ7RunOutcome.CONTROL_RECORDED
    written = len(rig.sink.events)

    second = runner.handle(ModelJoinJ7RunCommand())
    assert second.control_owed is False
    assert not any(i.control for i in second.items)
    assert len(rig.sink.events) == written

    jump.advance(timedelta(hours=OVERLAY.control.window_hours + 1))
    again = runner.handle(ModelJoinJ7RunCommand(probe=True))
    assert again.control_owed is True
    assert again.items == ()
    assert len(rig.sink.events) == written


def test_control_the_control_never_posts_on_the_test_head() -> None:
    rig = _Rig([])
    gapi = runtime_join_j7.build_rig(OVERLAY)
    rapi = runtime_join_j7.build_rig(OVERLAY)
    runner = rig.runner(golden=gapi, drill=rapi)

    result = runner.handle(ModelJoinJ7RunCommand())

    control = next(i for i in result.items if i.control)
    assert control.outcome is EnumJoinJ7RunOutcome.CONTROL_RECORDED
    # The control reads the head (a read is a call) and never posts: the golden twin holds no run, the drill's twin does.
    assert gapi.api.runs == ()
    assert rapi.api.runs


def test_the_overlay_refuses_a_control_that_enforces_or_names_a_refusal_code() -> None:
    enforce = {**BASE, "control": {**BASE["control"], "mode": "enforce"}}
    refusal_code = {
        **BASE,
        "control": {**BASE["control"], "failure_code": "upstream_x"},
    }
    same_node = {
        **BASE,
        "control": {**BASE["control"], "upstream_node": BASE["emission_node"]},
    }
    with pytest.raises(ValidationError, match="mode that does not refuse"):
        ModelJoinJ7Overlay.model_validate(enforce)
    with pytest.raises(ValidationError, match="not a refusal"):
        ModelJoinJ7Overlay.model_validate(refusal_code)
    with pytest.raises(ValidationError, match="not piece 8's emission node"):
        ModelJoinJ7Overlay.model_validate(same_node)


# --- the overlay and the runtime ---------------------------------------------------------------------------------------


def test_the_example_overlay_loads_and_holds_no_value() -> None:
    overlay = ModelJoinJ7Overlay.model_validate(BASE)
    assert overlay.allow_injected_break is False
    assert overlay.check_run.source.backend == "inmemory"


def test_the_overlay_refuses_one_piece_named_twice_and_one_head_named_twice() -> None:
    with pytest.raises(ValueError, match="two pieces"):
        ModelJoinJ7Overlay.model_validate(
            {**BASE, "other_piece": BASE["stopped_piece"]}
        )
    with pytest.raises(ValueError, match="two heads"):
        ModelJoinJ7Overlay.model_validate({**BASE, "old_head_sha": BASE["head_sha"]})
    missing = {k: v for k, v in BASE.items() if k != "head_sha"}
    with pytest.raises(ValueError, match="head_sha"):
        ModelJoinJ7Overlay.model_validate(missing)


def test_the_runtime_without_an_overlay_is_indeterminate_not_a_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))

    assert runtime_join_j7.main(["run"]) == 3
    assert "INDETERMINATE" in capsys.readouterr().err


def _write_roots(tmp_path: Path, *, downstream_node: str, allow: bool) -> Path:
    roots = tmp_path / "roots"
    bodies = {
        "node_lab_check_run_join_j7": {**BASE, "allow_injected_break": allow},
        "node_join_refusal_effect": _refusal_overlay(downstream_node),
    }
    for name, body in bodies.items():
        (roots / name).mkdir(parents=True)
        (roots / name / "overlay.yaml").write_text(
            yaml.safe_dump(body), encoding="utf-8"
        )
    return roots


def test_the_runtime_end_to_end_over_a_file_ledger_and_a_jsonl_journal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    now = datetime.now(UTC)
    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        _reading_row(now - timedelta(minutes=5), "WORKING") + "\n", encoding="utf-8"
    )
    roots = _write_roots(tmp_path, downstream_node=OVERLAY.emission_node, allow=False)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(roots))
    monkeypatch.setenv("J7_TEST_LEDGER", str(ledger))
    monkeypatch.setenv("J7_TEST_HOME", str(tmp_path))

    assert runtime_join_j7.main(["run"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["read_ok"] is True
    assert sorted(i["outcome"] for i in out["items"]) == [
        "control_recorded",
        "drill_recorded",
        "landed",
    ]
    lines = [
        json.loads(line)
        for line in (tmp_path / "emissions.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [line["phase"] for line in lines] == [
        "start",
        "success",
        "start",
        "failure",
        "failure",
        "start",
        "failure",
    ]
    control_cid = next(i["correlation_id"] for i in out["items"] if i["control"])
    piece7_failure = [
        line
        for line in lines
        if line["piece"] == FROM_PIECE
        and line["correlation_id"] == control_cid
        and line["phase"] == "failure"
    ]
    assert len(piece7_failure) == 1
    assert piece7_failure[0]["failure_code"] == OVERLAY.control.failure_code
    assert {line["node"] for line in lines} == {
        OVERLAY.emission_node,
        OVERLAY.control.upstream_node,
    }
    assert runtime_join_j7.main(["run", "--inject", "deaf-invalidation"]) == 3


def test_the_runtime_refuses_an_emission_node_that_is_not_the_refusal_overlays(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = tmp_path / "ledger.md"
    ledger.write_text("", encoding="utf-8")
    roots = _write_roots(
        tmp_path, downstream_node="example_node:join_j7_renamed", allow=False
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(roots))
    monkeypatch.setenv("J7_TEST_LEDGER", str(ledger))
    monkeypatch.setenv("J7_TEST_HOME", str(tmp_path))

    assert runtime_join_j7.main(["run"]) == 3
    assert "downstream_node" in capsys.readouterr().err
