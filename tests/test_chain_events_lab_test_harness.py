# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain events of the central test harness, piece 8 of the floor (OMN-23007).

The floor read piece 8 as "reading WORKING; no golden and error event yet" because the harness wrote a reading and no
chain event. Golden: a run that ends as required emits a start and a success, and the planted broken input going red
emits its own typed failure (a drill); the floor folds the two into piece 8's golden and error receipts and the row no
longer reads "no events yet". Error: each injected break ends the run chain in a typed failure code, and the planted
failure is emitted only when the broken input really went red. Failing control: a harness that drops the emission
leaves the floor reading "no events yet" for the same working reading.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers.emission_sink import (
    InMemoryEmissionSink,
    JsonlEmissionSink,
)
from omnimarket.models.floor_17a import (
    ModelFloorReceiptInput,
    ModelFloorSimple,
    ModelFloorSimpleTickRequest,
    ModelNodeEmission,
    ModelPieceDef,
)
from omnimarket.models.lab_test_harness import (
    ModelLabTestHarnessOverlay,
    ModelLabTestHarnessRequested,
)
from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
    HandlerFloorReceipt,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.handler_floor_simple_tick import (
    HandlerFloorSimpleTick,
)
from omnimarket.nodes.node_lab_test_harness_effect.handlers import (
    HandlerLabTestHarnessEffect,
    InMemoryLabTestHarnessStore,
)
from omnimarket.nodes.node_lab_test_harness_effect.handlers.handler_lab_test_harness_effect import (
    FAILURE_GOLDEN,
    FAILURE_INFRA,
    FAILURE_NOT_RED,
    FAILURE_PLANTED,
    FAILURE_RAISED,
    LabHarnessLedgerAppendError,
    sink_from_overlay,
)
from omnimarket.nodes.node_lab_test_harness_effect.handlers.protocol_lab_test_harness import (
    ProtocolLabTestRunner,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import ModelAppendOutcome
from tests.helpers.lab_test_harness_fakes import (
    T0,
    LiveLabBus,
    make_origin,
    overlay_document,
)
from tests.nodes.node_floor_simple_effect.support import (
    NOW,
    OVERLAYS,
    build_home,
    environ,
)

pytestmark = pytest.mark.unit

PIECE = 8
GO_RULINGS = (
    Path(__file__).parent
    / "nodes/node_floor_simple_effect/fixtures/ledger_rows_go_and_code_graph.txt"
)
CHAIN_NODE = "node_lab_test_harness_effect:run"
PLANTED_NODE = CHAIN_NODE + ":planted_failure"
LANE = "central-test-harness"
#: The rule the floor overlay carries for piece 8: a WORKING row that is no drill is golden, any NOT_WORKING row a failure.
PIECE_8_RULE = """
  - piece: 8
    node: "node_lab_test_harness_effect:run"
    golden:
      row_type: STATUS
      lane: central-test-harness
      cells: {src: central-test-harness, drill: "no", verdict: WORKING, outcome: read}
    error:
      row_type: STATUS
      lane: central-test-harness
      cells: {src: central-test-harness, verdict: NOT_WORKING}
    failure_code_cell: reason
    failure_default: harness_untyped
    error_drill_cell: drill
    lookback_days: 7
"""


class RecordingAppender:
    """The bus append command, in memory: the rows it was given and the request ids."""

    def __init__(self) -> None:
        self.rows: list[str] = []
        self.request_ids: list[str] = []

    def append(self, rows: str, request_id: str) -> ModelAppendOutcome:
        self.rows.append(rows.rstrip("\n"))
        self.request_ids.append(request_id)
        return ModelAppendOutcome(status="accepted")


class RefusingAppender(RecordingAppender):
    """The bus append command when the ledger host refuses the row."""

    def append(self, rows: str, request_id: str) -> ModelAppendOutcome:
        super().append(rows, request_id)
        return ModelAppendOutcome(status="refused", message="row-grammar")


@pytest.fixture(autouse=True)
def _host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEX_TEST_HOST", "h053")


def overlay(
    sha: str, tmp_path: Path, *, emission: bool = True, **kw: Any
) -> ModelLabTestHarnessOverlay:
    document = overlay_document(sha, tmp_path / "r.json", **kw)
    if emission:
        document["emission"] = {
            "home_env": "ONEX_TEST_HOME",
            "journal_relpath": ".onex_state/floor-simple/emissions.jsonl",
            "piece_number": PIECE,
            "chain_node": CHAIN_NODE,
            "ledger": {
                "lane": LANE,
                "actor": "node:node_lab_test_harness_effect",
                "command_env": "ONEX_TEST_APPEND_COMMAND",
                "host_env": "ONEX_TEST_HOST",
                "timeout_seconds": 30,
            },
        }
    return ModelLabTestHarnessOverlay.model_validate(document)


def request(run_id: str) -> ModelLabTestHarnessRequested:
    return ModelLabTestHarnessRequested(
        run_id=run_id, requested_at=T0, trigger="lab-test-harness"
    )


async def run_harness(
    tmp_path: Path,
    sink: InMemoryEmissionSink,
    run_id: str,
    appender: RecordingAppender | None = None,
    **kw: Any,
) -> None:
    url_base, sha = make_origin(tmp_path)
    live = LiveLabBus(url_base, tmp_path / "work")
    handler = HandlerLabTestHarnessEffect(
        overlay=overlay(sha, tmp_path, **kw),
        runner_opener=live.opener,
        store=InMemoryLabTestHarnessStore(),
        now=lambda: NOW - timedelta(minutes=5),
        sink=sink,
        appender=appender or RecordingAppender(),
    )
    await handler.handle(request(run_id))


def of(sink: InMemoryEmissionSink, node: str) -> list[ModelNodeEmission]:
    return [e for e in sink.events if e.node == node]


def fold(sink: InMemoryEmissionSink) -> Any:
    receipts = HandlerFloorReceipt().handle(
        ModelFloorReceiptInput(
            now=NOW,
            emissions=tuple(sink.events),
            pieces=(ModelPieceDef(piece=PIECE, name="CI", reading_max_age_s=86400),),
            joins=(),
            chain_deadline_s=900,
            freshness_max_age_s=86400,
            error_max_age_s=86400,
        )
    )
    return receipts.pieces[0], receipts


def admitting_piece_8(tmp_path: Path, *, rule: bool = False) -> Path:
    """The floor overlay with piece 8 admitted as step 0 (as the published overlay does), every other overlay linked."""
    root = tmp_path / "overlays-step0"
    source = OVERLAYS / "node_floor_simple_compute" / "overlay.yaml"
    target = root / "node_floor_simple_compute" / "overlay.yaml"
    target.parent.mkdir(parents=True)
    text = source.read_text(encoding="utf-8")
    assert "step0_pieces: [17]" in text
    text = text.replace("step0_pieces: [17]", "step0_pieces: [8, 17]")
    target.write_text(text + (PIECE_8_RULE if rule else ""), encoding="utf-8")
    for other in OVERLAYS.iterdir():
        if other.name != "node_floor_simple_compute":
            (root / other.name).symlink_to(other, target_is_directory=True)
    return root


def floor_row_text(
    tmp_path: Path,
    sink: InMemoryEmissionSink,
    rows: list[str] | None = None,
    *,
    rule: bool = False,
) -> str:
    home = build_home(tmp_path)
    env = {
        **environ(home),
        "ONEX_SKILL_OVERLAY_ROOTS": str(admitting_piece_8(tmp_path, rule=rule)),
    }
    handler = HandlerFloorSimpleTick(
        sink=sink,
        clock=lambda: NOW,
        served_alarm_ids=lambda: None,
        ledger_source=InMemoryLedger(
            [
                *(x for x in GO_RULINGS.read_text(encoding="utf-8").splitlines() if x),
                *(rows or []),
            ]
        ),
    )
    result = handler.run(
        ModelFloorSimpleTickRequest(home=str(home), now=NOW), environ=env
    )
    assert result.ok
    doc = ModelFloorSimple.model_validate_json(
        (home / ".claude_scratch/factory-floor/floor-simple.json").read_text(
            encoding="utf-8"
        )
    )
    return next(p for p in doc.pieces if p.piece == PIECE).text


async def test_golden_a_good_run_emits_a_golden_chain_and_a_planted_typed_error(
    tmp_path: Path,
) -> None:
    sink = InMemoryEmissionSink()
    await run_harness(tmp_path, sink, "run-golden")
    run_chain = of(sink, CHAIN_NODE)
    assert [e.phase for e in run_chain] == ["start", "success"]
    assert {e.piece for e in sink.events} == {PIECE}
    assert run_chain[1].counters == {"cases": 2}
    planted = of(sink, PLANTED_NODE)
    assert [e.phase for e in planted] == ["start", "failure"]
    assert planted[1].failure_code == FAILURE_PLANTED
    assert all(e.drill for e in planted)
    assert not any(e.drill for e in run_chain)
    piece, receipts = fold(sink)
    assert receipts.findings == ()
    assert piece.golden_at is not None
    assert piece.error_at is not None
    assert piece.started
    assert piece.freshness == "fresh"


async def test_golden_the_floor_row_of_piece_8_no_longer_reads_no_events_yet(
    tmp_path: Path,
) -> None:
    sink = InMemoryEmissionSink()
    await run_harness(tmp_path, sink, "run-floor")
    text = floor_row_text(tmp_path, sink)
    assert "no events yet" not in text
    assert "no golden and error event yet" not in text
    assert "golden" in text


async def test_control_a_harness_that_drops_the_emission_leaves_the_floor_at_no_events_yet(
    tmp_path: Path,
) -> None:
    """Failing control: the same good run with the emission dropped. The floor reads what it read before this change."""
    sink = InMemoryEmissionSink()
    await run_harness(tmp_path, sink, "run-dropped", emission=False)
    assert sink.events == []
    piece, _ = fold(sink)
    assert piece.golden_at is None
    assert piece.error_at is None
    assert not piece.started
    text = floor_row_text(tmp_path / "control", sink)
    assert text.startswith("reading WORKING") or "admitted" in text, text
    assert "no events yet" in text


async def test_error_the_golden_command_broken_ends_the_run_chain_in_a_typed_failure(
    tmp_path: Path,
) -> None:
    sink = InMemoryEmissionSink()
    await run_harness(tmp_path, sink, "run-break-golden", golden="fails.py")
    run_chain = of(sink, CHAIN_NODE)
    assert [e.phase for e in run_chain] == ["start", "failure"]
    assert run_chain[1].failure_code == FAILURE_GOLDEN
    piece, receipts = fold(sink)
    assert receipts.findings == ()
    assert piece.golden_at is None
    assert piece.error_at is not None


async def test_error_the_broken_input_made_good_emits_a_typed_failure_and_no_planted_error(
    tmp_path: Path,
) -> None:
    """Injected break: the planted failure does not go red, so no typed error of the planted chain exists to emit."""
    sink = InMemoryEmissionSink()
    await run_harness(tmp_path, sink, "run-break-error", error="passes.py")
    run_chain = of(sink, CHAIN_NODE)
    assert [e.phase for e in run_chain] == ["start", "failure"]
    assert run_chain[1].failure_code == FAILURE_NOT_RED
    assert of(sink, PLANTED_NODE) == []


async def test_error_a_silent_lab_host_ends_the_run_chain_in_an_infrastructure_failure(
    tmp_path: Path,
) -> None:
    url_base, sha = make_origin(tmp_path)
    live = LiveLabBus(url_base, tmp_path / "work", serve=False)
    base = overlay(sha, tmp_path, receipt_slack_seconds=0)
    short = base.model_copy(
        update={
            "cases": tuple(
                c.model_copy(update={"timeout_seconds": 1}) for c in base.cases
            )
        }
    )
    sink = InMemoryEmissionSink()
    handler = HandlerLabTestHarnessEffect(
        overlay=short,
        runner_opener=live.opener,
        store=InMemoryLabTestHarnessStore(),
        now=lambda: NOW,
        sink=sink,
        appender=RecordingAppender(),
    )
    await handler.handle(request("run-silent"))
    run_chain = of(sink, CHAIN_NODE)
    assert run_chain[-1].failure_code == FAILURE_INFRA
    assert of(sink, PLANTED_NODE) == []


async def test_error_a_run_that_raises_closes_its_chain_with_a_typed_failure(
    tmp_path: Path,
) -> None:
    @asynccontextmanager
    async def open_down(
        overlay: ModelLabTestHarnessOverlay,
    ) -> AsyncIterator[ProtocolLabTestRunner]:
        raise RuntimeError("bus down")
        yield

    _, sha = make_origin(tmp_path)
    sink = InMemoryEmissionSink()
    handler = HandlerLabTestHarnessEffect(
        overlay=overlay(sha, tmp_path),
        runner_opener=open_down,
        store=InMemoryLabTestHarnessStore(),
        now=lambda: NOW,
        sink=sink,
    )
    with pytest.raises(RuntimeError):
        await handler.handle(request("run-raises"))
    run_chain = of(sink, CHAIN_NODE)
    assert [e.phase for e in run_chain] == ["start", "failure"]
    assert run_chain[1].failure_code == FAILURE_RAISED


def test_the_overlay_names_the_journal_and_refuses_without_its_home(
    tmp_path: Path,
) -> None:
    ov = overlay("a" * 40, tmp_path)
    sink = sink_from_overlay(ov, {"ONEX_TEST_HOME": str(tmp_path)})
    assert isinstance(sink, JsonlEmissionSink)
    with pytest.raises(Exception, match="ONEX_TEST_HOME"):
        sink_from_overlay(ov, {})
    assert sink_from_overlay(overlay("a" * 40, tmp_path, emission=False), {}) is None


async def test_golden_the_journal_the_floor_reads_receives_the_chains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url_base, sha = make_origin(tmp_path)
    live = LiveLabBus(url_base, tmp_path / "work")
    monkeypatch.setenv("ONEX_TEST_HOME", str(tmp_path / "home"))
    handler = HandlerLabTestHarnessEffect(
        overlay=overlay(sha, tmp_path),
        runner_opener=live.opener,
        store=InMemoryLabTestHarnessStore(),
        appender=RecordingAppender(),
    )
    await handler.handle(request("run-journal"))
    journal = tmp_path / "home/.onex_state/floor-simple/emissions.jsonl"
    rows = [json.loads(x) for x in journal.read_text(encoding="utf-8").splitlines()]
    assert [(r["node"], r["phase"]) for r in rows] == [
        (CHAIN_NODE, "start"),
        (CHAIN_NODE, "success"),
        (PLANTED_NODE, "start"),
        (PLANTED_NODE, "failure"),
    ]


async def test_golden_the_ledger_rows_of_a_good_run_are_what_a_floor_on_another_host_reads(
    tmp_path: Path,
) -> None:
    appender = RecordingAppender()
    await run_harness(
        tmp_path, InMemoryEmissionSink(), "run-rows", appender=appender, emission=True
    )
    assert len(appender.rows) == 2
    golden, planted = appender.rows
    for cell in (
        f"lane={LANE}",
        f"src={LANE}",
        "verdict=WORKING",
        "drill=no",
        "outcome=read",
        "piece=8",
        "host=h053",
        "reason=ok",
    ):
        assert f" | {cell}" in golden, cell
    for cell in ("verdict=NOT_WORKING", "drill=yes", f"reason={FAILURE_PLANTED}"):
        assert f" | {cell}" in planted, cell
    assert len(set(appender.request_ids)) == 2
    # The floor with the piece 8 rule in its overlay draws golden and error ages from the rows alone.
    text = floor_row_text(
        tmp_path / "rows", InMemoryEmissionSink(), appender.rows, rule=True
    )
    assert "no events yet" not in text
    assert "golden" in text
    assert "golden never" not in text


async def test_control_the_same_rows_without_the_floor_rule_leave_the_row_at_no_events_yet(
    tmp_path: Path,
) -> None:
    """Failing control: the floor overlay of before this change has no rule for piece 8, so its rows are no events."""
    appender = RecordingAppender()
    await run_harness(tmp_path, InMemoryEmissionSink(), "run-rows", appender=appender)
    text = floor_row_text(
        tmp_path / "rows", InMemoryEmissionSink(), appender.rows, rule=False
    )
    assert "no events yet" in text


async def test_error_a_broken_golden_command_writes_a_not_working_row_with_its_typed_reason(
    tmp_path: Path,
) -> None:
    appender = RecordingAppender()
    await run_harness(
        tmp_path,
        InMemoryEmissionSink(),
        "run-rows-bad",
        appender=appender,
        golden="fails.py",
    )
    run_row = appender.rows[0]
    assert " | verdict=NOT_WORKING" in run_row
    assert " | drill=no" in run_row
    assert f" | reason={FAILURE_GOLDEN}" in run_row
    text = floor_row_text(
        tmp_path / "rows", InMemoryEmissionSink(), appender.rows, rule=True
    )
    assert "golden never" in text
    assert "no events yet" not in text


async def test_error_a_ledger_host_that_refuses_the_rows_fails_the_run_instead_of_passing_silently(
    tmp_path: Path,
) -> None:
    appender = RefusingAppender()
    with pytest.raises(LabHarnessLedgerAppendError, match="refused"):
        await run_harness(
            tmp_path, InMemoryEmissionSink(), "run-rows-refused", appender=appender
        )
    assert len(appender.rows) == 2


async def test_replay_a_repeated_run_id_keeps_the_first_runs_chains_valid(
    tmp_path: Path,
) -> None:
    """The same run_id delivered twice (the supported `run --run-id` replay) must not write a second start.

    A second start under one correlation id reads as `duplicate_start`, which drops the golden and the error receipt.
    """
    sink = InMemoryEmissionSink()
    await run_harness(tmp_path / "first", sink, "run-replay")
    await run_harness(tmp_path / "second", sink, "run-replay")
    assert [e.phase for e in of(sink, CHAIN_NODE)] == ["start", "success"]
    assert [e.phase for e in of(sink, PLANTED_NODE)] == ["start", "failure"]
    piece, receipts = fold(sink)
    assert receipts.findings == ()
    assert piece.golden_at is not None
    assert piece.error_at is not None
