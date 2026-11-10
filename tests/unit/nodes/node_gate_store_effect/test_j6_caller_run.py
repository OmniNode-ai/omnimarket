# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""J6 runner (OMN-22936): golden and error chain event tests and the failing controls, judged by the floor's own fold.

Before this run the J6 join handler (``HandlerGateStoreJoinJ6``) was called by nothing: the floor read J6 ``not_run``. The
runner answers each piece 6 golden reading with a stop in the gate store and piece 7's chain under the reading's correlation
id; the join refusal node answers each piece 6 failure. The golden case reads ``golden_only`` and, with the refusal,
``passed``. The fence drill is piece 7's own error chain.

Failing controls: the run removed leaves the join broken (a piece 6 failure nobody answers); the named break
``drop-audit-row`` leaves the join not golden with the typed failure on the journal; a drill that does not hold writes no error
receipt.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.models.floor_17a import (
    ModelFloorReceiptInput,
    ModelJoinDef,
    ModelNodeEmission,
    ModelPieceDef,
    ModelPieceEventRule,
)
from omnimarket.models.model_gate_store_join_j6 import EnumJoinJ6Outcome
from omnimarket.models.model_gate_store_join_j6_run import (
    EnumJoinJ6RunOutcome,
    ModelJoinJ6RunCommand,
)
from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
    HandlerFloorReceipt,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.ledger_reads import (
    piece_events,
)
from omnimarket.nodes.node_gate_store_effect import runtime_join_j6
from omnimarket.nodes.node_gate_store_effect.handlers.handler_gate_store_join_j6 import (
    load_join_j6_policy,
)
from omnimarket.nodes.node_gate_store_effect.handlers.handler_gate_store_join_j6_run import (
    HandlerGateStoreJoinJ6Run,
    JoinJ6Rig,
)
from omnimarket.nodes.node_gate_store_effect.overlay_join_j6 import (
    ModelJoinJ6Overlay,
)
from omnimarket.nodes.node_join_refusal_effect import HandlerJoinRefusal
from omnimarket.nodes.node_join_refusal_effect.models import ModelJoinRefusalCommand
from omnimarket.nodes.node_join_refusal_effect.overlay import ModelJoinRefusalOverlay
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
    evidence_row_from_text,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 9, 12, 0, 0, tzinfo=UTC)
POLICY = load_join_j6_policy()
LANE = "example-ledger-reading"
GOLDEN_AT = NOW - timedelta(minutes=30)
ERROR_AT = NOW - timedelta(minutes=45)
OVERLAY = ModelJoinJ6Overlay(
    emission_node="example_gate_store_node:join_j6",
    stopped_piece="example_piece_stopped",
    other_piece="example_piece_other",
    piece_limit=3,
    reservation_ttl_seconds=60,
    max_chains_per_run=1,
    drill_window_hours=6,
    drill_commit_deadline_seconds=0.05,
    drill_silence_seconds=1.0,
    allow_injected_break=True,
    injected_fault="Swap the audit sink for one that keeps nothing",
)
RULE = ModelPieceEventRule.model_validate(
    {
        "piece": 6,
        "node": "example_ledger_node:read",
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
        "failure_default": "ledger_reading_failed",
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


def _reading_row(at: datetime, verdict: str, *, drill: str = "no") -> str:
    stamp = at.strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f"{stamp} | STATUS | lane={LANE} | src={LANE} | verdict={verdict}"
        f" | reason=example_failure | drill={drill} | probed_at={stamp}"
    )


class _Rig:
    """Piece 6's rows on the ledger source, the journal in memory, the gate store the in-memory twin."""

    def __init__(self, rows: list[str]) -> None:
        self.source = InMemoryLedger(rows)
        self.sink = InMemoryEmissionSink()
        self.clock = _Clock(NOW)

    def runner(
        self,
        *,
        golden: JoinJ6Rig | None = None,
        drill: JoinJ6Rig | None = None,
        drop_audit: bool = False,
        overlay: ModelJoinJ6Overlay = OVERLAY,
    ) -> HandlerGateStoreJoinJ6Run:
        return HandlerGateStoreJoinJ6Run(
            source=self.source,
            sink=self.sink,
            upstream=RULE,
            overlay=overlay,
            golden_rig=lambda: (
                golden or runtime_join_j6.build_rig(overlay, drop_audit=drop_audit)
            ),
            drill_rig=lambda: (
                drill
                or runtime_join_j6.build_rig(
                    overlay, silent=True, drop_audit=drop_audit
                )
            ),
            window_hours=48,
            to_piece=POLICY.to_piece,
            backend="in_memory",
            clock=self.clock,
        )

    def piece7(self) -> list[ModelNodeEmission]:
        return [e for e in self.sink.events if e.piece == POLICY.to_piece]


def _fold(emissions: list[ModelNodeEmission], now: datetime) -> tuple[str, list[str]]:
    receipts = HandlerFloorReceipt().handle(
        ModelFloorReceiptInput(
            now=now,
            emissions=tuple(emissions),
            pieces=tuple(
                ModelPieceDef(piece=p, name=f"piece {p}", reading_max_age_s=86400)
                for p in (POLICY.from_piece, POLICY.to_piece)
            ),
            joins=(
                ModelJoinDef(
                    id=POLICY.join_id,
                    from_piece=POLICY.from_piece,
                    to_piece=POLICY.to_piece,
                    label="ledger to process spine",
                ),
            ),
            freshness_max_age_s=86400,
            error_max_age_s=86400,
            chain_deadline_s=900,
        )
    )
    join = next(j for j in receipts.joins if j.join == POLICY.join_id)
    return join.state, [f.code for f in receipts.findings]


def _floor_read(rig: _Rig, now: datetime) -> tuple[str, list[str]]:
    """What the floor folds: piece 6's chains from the ledger rows, plus every emission on the journal."""
    rows = [r for line in rig.source.lines if (r := evidence_row_from_text(line))]
    return _fold([*piece_events(rows, (RULE,)), *rig.sink.events], now)


def _refuse(rig: _Rig, now: datetime) -> None:
    """The join refusal node, as the publish cycle runs it, over the same rows and the same journal."""
    overlay = ModelJoinRefusalOverlay.model_validate(
        {
            "ledger_read": {
                "route": "file",
                "path_env": "UNUSED",
                "timeout_seconds": 60,
                "row_limit": 5000,
            },
            "emission": {
                "home_env": "UNUSED",
                "journal_relpath": "unused.jsonl",
                "window_hours": 48,
            },
            "joins": [
                {
                    "join": "J6",
                    "from_piece": 6,
                    "to_piece": 7,
                    "downstream_node": "example_gate_store_node:join_j6",
                    "injected_fault": "Drop the piece 7 refusal from the emission journal",
                    "upstream": RULE.model_dump(mode="json"),
                }
            ],
        }
    )
    HandlerJoinRefusal(
        overlay=overlay,
        source=rig.source,
        sink=rig.sink,
        backend="in_memory",
        clock=lambda: now,
    ).handle(ModelJoinRefusalCommand(write=True))


# --- the golden chain ------------------------------------------------------------------------------------------------


def test_golden_run_stops_a_test_piece_audits_it_and_the_floor_reads_golden_only() -> (
    None
):
    rig = _Rig([_reading_row(GOLDEN_AT, "WORKING")])

    result = rig.runner().handle(ModelJoinJ6RunCommand())

    assert result.read_ok
    assert (result.owed, result.answered) == (1, 0)
    landed = [i for i in result.items if not i.drill]
    assert [i.outcome for i in landed] == [EnumJoinJ6RunOutcome.LANDED]
    chain = [e for e in rig.piece7() if e.correlation_id == landed[0].correlation_id]
    assert [e.phase for e in chain] == ["start", "success"]
    assert chain[-1].counters == {
        "audit_rows": 1,
        "projected_rows": 1,
        "sends_refused": 1,
        "sends_admitted": 1,
    }
    assert all(e.backend == "in_memory" and not e.drill for e in chain)
    assert _floor_read(rig, NOW + timedelta(minutes=5)) == ("golden_only", [])


def test_golden_and_error_together_read_passed() -> None:
    rig = _Rig(
        [_reading_row(GOLDEN_AT, "WORKING"), _reading_row(ERROR_AT, "NOT_WORKING")]
    )

    rig.runner().handle(ModelJoinJ6RunCommand())
    _refuse(rig, NOW + timedelta(minutes=2))

    assert _floor_read(rig, NOW + timedelta(minutes=5)) == ("passed", [])


def test_golden_a_rerun_stops_nothing_for_a_chain_piece_7_already_answered() -> None:
    rig = _Rig([_reading_row(GOLDEN_AT, "WORKING")])
    runner = rig.runner()
    runner.handle(ModelJoinJ6RunCommand())
    written = len(rig.sink.events)

    again = runner.handle(ModelJoinJ6RunCommand())

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

    result = rig.runner().handle(ModelJoinJ6RunCommand())

    chains = [i for i in result.items if not i.drill]
    assert (result.owed, len(chains)) == (2, 1)
    newest_row = evidence_row_from_text(rig.source.lines[1])
    assert newest_row is not None
    assert (
        chains[0].correlation_id
        == piece_events([newest_row], (RULE,))[0].correlation_id
    )


# --- the error chain: the fence drill --------------------------------------------------------------------------------


def test_error_the_fence_drill_writes_piece_7s_own_error_chain_once_per_window() -> (
    None
):
    rig = _Rig([])
    runner = rig.runner()

    first = runner.handle(ModelJoinJ6RunCommand())
    second = runner.handle(ModelJoinJ6RunCommand())

    assert [i.outcome for i in first.items] == [EnumJoinJ6RunOutcome.DRILL_RECORDED]
    drill = first.items[0]
    assert drill.drill
    assert drill.failure_code == "fence_closed_stop_unanswered"
    assert "no answer within" in drill.detail
    chain = rig.piece7()
    assert [(e.phase, e.drill) for e in chain] == [("start", True), ("failure", True)]
    assert chain[-1].failure_code == "fence_closed_stop_unanswered"
    assert second.drill_owed is False
    assert second.items == ()
    assert len(rig.piece7()) == 2
    # A drill chain is piece 7's error receipt; it is no evidence for the join.
    assert _floor_read(rig, NOW + timedelta(minutes=5)) == ("not_run", [])


def test_error_a_failed_or_drill_reading_is_never_answered_by_the_runner() -> None:
    rig = _Rig(
        [
            _reading_row(ERROR_AT, "NOT_WORKING"),
            _reading_row(ERROR_AT, "NOT_WORKING", drill="yes"),
        ]
    )

    result = rig.runner().handle(ModelJoinJ6RunCommand())

    assert result.owed == 0
    assert [i.drill for i in result.items] == [True]


def test_error_an_unreadable_ledger_is_not_an_empty_answer() -> None:
    rig = _Rig([_reading_row(GOLDEN_AT, "WORKING")])
    rig.source.read_error = "ledger-file-unreadable:OSError"

    result = rig.runner().handle(ModelJoinJ6RunCommand())

    assert result.read_ok is False
    assert "unread" in result.reason
    assert rig.sink.events == []


def test_a_probe_reads_what_is_owed_and_writes_nothing() -> None:
    rig = _Rig([_reading_row(GOLDEN_AT, "WORKING")])

    result = rig.runner().handle(ModelJoinJ6RunCommand(probe=True))

    assert (result.owed, result.drill_owed, result.items) == (1, True, ())
    assert rig.sink.events == []


# --- the failing controls ----------------------------------------------------------------------------------------------


def test_control_without_the_run_and_the_refusal_the_join_reads_broken() -> None:
    """The reading a piece 6 failure gets with piece 7 silent past the deadline."""
    rig = _Rig([_reading_row(ERROR_AT, "NOT_WORKING")])

    state, findings = _floor_read(rig, NOW + timedelta(hours=1))

    assert state == "broken"
    assert findings == ["downstream_missing_typed_failure"]


def test_control_the_named_break_drop_audit_row_leaves_the_join_not_golden() -> None:
    rig = _Rig([_reading_row(GOLDEN_AT, "WORKING")])

    result = rig.runner(drop_audit=True).handle(ModelJoinJ6RunCommand())

    chains = [i for i in result.items if not i.drill]
    assert [i.outcome for i in chains] == [EnumJoinJ6RunOutcome.STOP_NOT_COMMITTED]
    assert chains[0].failure_code == POLICY.audit_row_missing_failure_code
    assert result.ok is False
    chain = [e for e in rig.piece7() if e.correlation_id == chains[0].correlation_id]
    assert [e.phase for e in chain] == ["start", "failure"]
    assert chain[-1].failure_code == POLICY.audit_row_missing_failure_code
    state, _ = _floor_read(rig, NOW + timedelta(minutes=5))
    assert state != "golden_only"
    assert state == "not_run"


def test_control_a_drill_that_does_not_hold_writes_no_error_receipt() -> None:
    """The twin answers the stop at once, so the stop commits and no silence closes the fence: not the drill's case."""
    rig = _Rig([])
    healthy = runtime_join_j6.build_rig(OVERLAY)

    result = rig.runner(drill=healthy).handle(ModelJoinJ6RunCommand())

    assert [i.outcome for i in result.items] == [EnumJoinJ6RunOutcome.DRILL_NOT_HELD]
    assert result.ok is False
    assert rig.sink.events == []


def test_control_a_projection_that_never_hears_the_row_is_a_typed_failure() -> None:
    from omnimarket.nodes.node_gate_store_effect.handlers.adapters_join_j6 import (
        InMemoryJoinJ6Projection,
    )

    class Deaf(InMemoryJoinJ6Projection):
        def project(self, row: object) -> None:
            return None

    healthy = runtime_join_j6.build_rig(OVERLAY)
    deaf = JoinJ6Rig(
        type(healthy.join)(
            runtime_join_j6._store(OVERLAY),
            healthy.audit,
            Deaf(),
            POLICY,
        ),
        healthy.audit,
        Deaf(),
    )
    rig = _Rig([_reading_row(GOLDEN_AT, "WORKING")])

    result = rig.runner(golden=deaf).handle(ModelJoinJ6RunCommand())

    chains = [i for i in result.items if not i.drill]
    assert [i.outcome for i in chains] == [EnumJoinJ6RunOutcome.STOP_NOT_COMMITTED]
    assert chains[0].failure_code == POLICY.projected_row_missing_failure_code
    assert _floor_read(rig, NOW + timedelta(minutes=5))[0] == "not_run"


def test_the_send_of_the_stopped_piece_is_refused_by_the_store_itself() -> None:
    rig = runtime_join_j6.build_rig(OVERLAY)
    from omnimarket.models.model_gate_store_join_j6 import (
        EnumJoinJ6Operation,
        ModelJoinJ6Request,
    )

    def req(op: EnumJoinJ6Operation, step: str) -> ModelJoinJ6Request:
        return ModelJoinJ6Request(
            correlation_id="c",
            operation=op,
            piece=OVERLAY.stopped_piece,
            step_key=step,
            reservation_id="r",
        )

    assert rig.join.handle(req(EnumJoinJ6Operation.STOP, "s")).outcome is (
        EnumJoinJ6Outcome.STOP_COMMITTED
    )
    refused = rig.join.handle(req(EnumJoinJ6Operation.SEND, "x"))
    assert refused.outcome is EnumJoinJ6Outcome.SEND_REFUSED
    assert refused.store_outcome == "refused_stop"


# --- the overlay and the runtime ---------------------------------------------------------------------------------------


def test_the_example_overlay_loads_and_holds_no_value() -> None:
    example = (
        Path(__file__).resolve().parents[4]
        / "src/omnimarket/nodes/node_gate_store_effect/overlay_join_j6.example.yaml"
    )
    overlay = ModelJoinJ6Overlay.model_validate(
        yaml.safe_load(example.read_text(encoding="utf-8"))
    )
    assert overlay.allow_injected_break is False
    assert overlay.drill_silence_seconds > overlay.drill_commit_deadline_seconds


def test_the_overlay_refuses_one_piece_named_twice_and_a_silence_shorter_than_the_deadline() -> (
    None
):
    base = OVERLAY.model_dump()
    with pytest.raises(ValueError, match="two pieces"):
        ModelJoinJ6Overlay.model_validate(
            {**base, "other_piece": base["stopped_piece"]}
        )
    with pytest.raises(ValueError, match="outlasts"):
        ModelJoinJ6Overlay.model_validate({**base, "drill_silence_seconds": 0.01})


def test_the_runtime_without_an_overlay_is_indeterminate_not_a_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))

    assert runtime_join_j6.main(["run"]) == 3
    assert "INDETERMINATE" in capsys.readouterr().err


def test_the_runtime_end_to_end_over_a_file_ledger_and_a_jsonl_journal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    roots = tmp_path / "roots"
    now = datetime.now(UTC)
    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        _reading_row(now - timedelta(minutes=5), "WORKING") + "\n", encoding="utf-8"
    )
    for name, body in {
        "node_gate_store_join_j6": {
            **OVERLAY.model_dump(),
            "allow_injected_break": False,
        },
        "node_join_refusal_effect": {
            "ledger_read": {
                "route": "file",
                "path_env": "J6_TEST_LEDGER",
                "archive_subdir": "archive",
                "timeout_seconds": 60,
                "row_limit": 5000,
            },
            "emission": {
                "home_env": "J6_TEST_HOME",
                "journal_relpath": "emissions.jsonl",
                "window_hours": 48,
            },
            "joins": [
                {
                    "join": "J6",
                    "from_piece": 6,
                    "to_piece": 7,
                    "downstream_node": OVERLAY.emission_node,
                    "injected_fault": "Drop the piece 7 refusal from the journal",
                    "upstream": RULE.model_dump(mode="json"),
                }
            ],
        },
    }.items():
        (roots / name).mkdir(parents=True)
        (roots / name / "overlay.yaml").write_text(
            yaml.safe_dump(body), encoding="utf-8"
        )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(roots))
    monkeypatch.setenv("J6_TEST_LEDGER", str(ledger))
    monkeypatch.setenv("J6_TEST_HOME", str(tmp_path))

    assert runtime_join_j6.main(["run"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["read_ok"] is True
    assert sorted(i["outcome"] for i in out["items"]) == ["drill_recorded", "landed"]
    lines = (tmp_path / "emissions.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["phase"] for line in lines] == [
        "start",
        "success",
        "start",
        "failure",
    ]
    assert runtime_join_j6.main(["run", "--inject", "drop-audit-row"]) == 3
