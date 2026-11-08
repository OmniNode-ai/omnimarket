# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The J16 join run by the publish cycle (OMN-23122): golden and error chain event tests, and the failing controls.

J16 is piece 16 (context, memory and evals) to piece 17 (the floor): an eval result appears on the floor with its age. The
node reads the newest reading row of piece 16 on the ledger and the floor record, and writes the join's two chains under one
correlation id. The floor's own receipt fold then judges the join.

Golden: a fresh eval result that the floor record shows with an age reads ``golden_only`` and the run reads ``golden``.
Error: a missing, UNKNOWN or stale eval result is piece 16's typed failure and piece 17's typed ``upstream_`` refusal (the join
reads ``error_only`` with no finding); a floor record with no age for piece 16 is piece 17's typed failure; an unreadable
ledger writes nothing. Failing controls: the piece 17 chain dropped from the journal, and the floor record that never carries
the result, each turn the golden join into something that is not golden.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.emission_sink import (
    InMemoryEmissionSink,
    JsonlEmissionSink,
)
from omnimarket.models.floor_17a import (
    ModelFloorReceiptInput,
    ModelFloorReceipts,
    ModelJoinDef,
    ModelNodeEmission,
    ModelPieceDef,
)
from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
    HandlerFloorReceipt,
)
from omnimarket.nodes.node_join_j16_effect import HandlerJoinJ16
from omnimarket.nodes.node_join_j16_effect.handlers.handler_join_j16 import (
    FAIL_NO_READING,
    FAIL_NO_RESULT,
    FAIL_NOT_DRAWN,
    FAIL_RECORD_UNREADABLE,
    FAIL_STALE,
)
from omnimarket.nodes.node_join_j16_effect.models import ModelJoinJ16Command
from omnimarket.nodes.node_join_j16_effect.overlay import ModelJoinJ16Overlay
from omnimarket.nodes.node_join_j16_effect.runtime_join_j16 import main
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    FileLedgerRowSource,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelLedgerQuery,
    ModelLedgerRead,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 8, 8, 0, tzinfo=UTC)
EXAMPLE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_join_j16_effect/overlay.example.yaml"
)


def _overlay() -> ModelJoinJ16Overlay:
    return ModelJoinJ16Overlay.model_validate(
        yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    )


def _stamp(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def _reading(
    probed: datetime,
    verdict: str,
    *,
    piece: int = 16,
    max_age: int = 7200,
    reason: str = "ok",
) -> str:
    return (
        f"{_stamp(probed)} | STATUS | lane=example-reading-writer | src=reading | piece={piece} | verdict={verdict} "
        f"| probed_at={_stamp(probed)} | published_at={_stamp(probed)} | max_age={max_age} | reason={reason}"
    )


def _ledger(tmp_path: Path, *rows: str) -> FileLedgerRowSource:
    live = tmp_path / "LEDGER.md"
    live.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return FileLedgerRowSource(live, None)


def _floor_record(tmp_path: Path, pieces: list[dict[str, object]] | None) -> Path:
    path = tmp_path / "floor-simple.json"
    if pieces is not None:
        path.write_text(json.dumps({"pieces": pieces}), encoding="utf-8")
    return path


def _drawn(tmp_path: Path) -> Path:
    return _floor_record(tmp_path, [{"piece": 16, "reading_age_s": 600}])


class _Unreadable:
    def read(self, query: ModelLedgerQuery) -> ModelLedgerRead:
        return ModelLedgerRead(error="ledger-file-unreadable:OSError")


class _DropsPiece(InMemoryEmissionSink):
    """The failing control: a journal that loses one piece's emissions."""

    def __init__(self, piece: int) -> None:
        super().__init__()
        self._piece = piece

    def emit(self, event: ModelNodeEmission) -> None:
        if event.piece != self._piece:
            super().emit(event)


def _handler(source, sink, floor_record: Path) -> HandlerJoinJ16:  # type: ignore[no-untyped-def]
    return HandlerJoinJ16(
        overlay=_overlay(),
        source=source,
        sink=sink,
        floor_record_path=floor_record,
        backend="in_memory",
        clock=lambda: NOW,
    )


def _fold(sink: InMemoryEmissionSink) -> ModelFloorReceipts:
    return HandlerFloorReceipt().handle(
        ModelFloorReceiptInput(
            now=NOW + timedelta(seconds=1),
            emissions=tuple(sink.events),
            pieces=tuple(
                ModelPieceDef(piece=n, name=f"piece {n}", reading_max_age_s=86400)
                for n in range(1, 19)
            ),
            joins=(
                ModelJoinDef(
                    id="J16", from_piece=16, to_piece=17, label="evals to the floor"
                ),
            ),
            freshness_max_age_s=7200,
            error_max_age_s=86400,
            chain_deadline_s=900,
            judged_to_pieces=(16, 17),
        )
    )


def _state(sink: InMemoryEmissionSink) -> tuple[str, list[str]]:
    receipts = _fold(sink)
    return receipts.joins[0].state, [f.code for f in receipts.findings]


def _run(tmp_path: Path, row: str | None, floor_record: Path | None = None):  # type: ignore[no-untyped-def]
    rows = (row,) if row else ()
    sink = InMemoryEmissionSink()
    result = _handler(
        _ledger(tmp_path, *rows), sink, floor_record or _drawn(tmp_path)
    ).handle(ModelJoinJ16Command(write=True))
    return result, sink


class TestGoldenChain:
    def test_j16_golden_a_fresh_eval_result_drawn_with_an_age_reads_golden(
        self, tmp_path: Path
    ) -> None:
        result, sink = _run(tmp_path, _reading(NOW - timedelta(minutes=10), "WORKING"))
        assert (result.outcome, result.wrote, result.reading_age_s) == (
            "golden",
            True,
            600,
        )
        assert [(e.piece, e.phase) for e in sink.events] == [
            (16, "start"),
            (16, "success"),
            (17, "start"),
            (17, "success"),
        ]
        assert {e.correlation_id for e in sink.events} == {result.correlation_id}
        assert _state(sink) == ("golden_only", [])

    def test_j16_golden_a_result_that_reads_not_working_still_reaches_the_floor(
        self, tmp_path: Path
    ) -> None:
        """The join tests that a result reaches the floor; the piece's own verdict is drawn by the piece."""
        result, sink = _run(
            tmp_path, _reading(NOW - timedelta(minutes=10), "NOT_WORKING")
        )
        assert result.outcome == "golden"
        assert _state(sink) == ("golden_only", [])

    def test_j16_golden_with_an_error_pair_the_join_passes(
        self, tmp_path: Path
    ) -> None:
        _, sink = _run(tmp_path, _reading(NOW - timedelta(minutes=10), "WORKING"))
        bad = _handler(
            _ledger(tmp_path),
            sink,
            _drawn(tmp_path),
        )
        assert bad.handle(ModelJoinJ16Command(write=True)).outcome == "error"
        assert _state(sink) == ("passed", [])

    def test_j16_probe_reads_and_writes_nothing(self, tmp_path: Path) -> None:
        sink = InMemoryEmissionSink()
        result = _handler(
            _ledger(tmp_path, _reading(NOW - timedelta(minutes=10), "WORKING")),
            sink,
            _drawn(tmp_path),
        ).handle(ModelJoinJ16Command(write=False))
        assert (result.outcome, result.wrote) == ("golden", False)
        assert sink.events == []


class TestErrorChain:
    @pytest.mark.parametrize(
        ("row", "code"),
        [
            (None, FAIL_NO_READING),
            (_reading(NOW - timedelta(minutes=10), "UNKNOWN"), FAIL_NO_RESULT),
            (_reading(NOW - timedelta(hours=3), "WORKING"), FAIL_STALE),
            (
                _reading(NOW - timedelta(minutes=10), "WORKING", piece=15),
                FAIL_NO_READING,
            ),
        ],
    )
    def test_j16_error_no_fresh_eval_result_is_piece_16_failing_and_piece_17_refusing(
        self, tmp_path: Path, row: str | None, code: str
    ) -> None:
        result, sink = _run(tmp_path, row)
        assert (result.outcome, result.failed_side, result.failure_code) == (
            "error",
            "from",
            code,
        )
        assert [(e.piece, e.phase, e.failure_code) for e in sink.events] == [
            (16, "start", None),
            (16, "failure", code),
            (17, "failure", f"upstream_{code}"),
        ]
        assert _state(sink) == ("error_only", [])

    def test_j16_error_the_reason_the_writer_gave_is_in_the_detail(
        self, tmp_path: Path
    ) -> None:
        result, _ = _run(
            tmp_path,
            _reading(NOW - timedelta(minutes=10), "UNKNOWN", reason="no-producer-yet"),
        )
        assert "no-producer-yet" in result.detail

    @pytest.mark.parametrize(
        ("pieces", "code"),
        [
            ([{"piece": 16, "reading_age_s": None}], FAIL_NOT_DRAWN),
            ([{"piece": 15, "reading_age_s": 5}], FAIL_NOT_DRAWN),
            ([{"piece": 16, "reading_age_s": True}], FAIL_NOT_DRAWN),
            (None, FAIL_RECORD_UNREADABLE),
        ],
    )
    def test_j16_error_a_floor_record_that_shows_no_age_is_piece_17_failing(
        self, tmp_path: Path, pieces: list[dict[str, object]] | None, code: str
    ) -> None:
        result, sink = _run(
            tmp_path,
            _reading(NOW - timedelta(minutes=10), "WORKING"),
            _floor_record(tmp_path, pieces),
        )
        assert (result.outcome, result.failed_side, result.failure_code) == (
            "error",
            "to",
            code,
        )
        assert [(e.piece, e.phase) for e in sink.events] == [
            (16, "start"),
            (16, "success"),
            (17, "start"),
            (17, "failure"),
        ]
        assert _state(sink)[0] != "golden_only"

    def test_j16_error_an_unreadable_ledger_writes_nothing(
        self, tmp_path: Path
    ) -> None:
        sink = InMemoryEmissionSink()
        result = _handler(_Unreadable(), sink, _drawn(tmp_path)).handle(
            ModelJoinJ16Command(write=True)
        )
        assert (result.outcome, result.wrote) == ("unread", False)
        assert "ledger unread" in result.detail
        assert sink.events == []


class TestFailingControl:
    def test_j16_control_with_the_piece_17_chain_dropped_the_join_is_not_golden(
        self, tmp_path: Path
    ) -> None:
        sink = _DropsPiece(17)
        _handler(
            _ledger(tmp_path, _reading(NOW - timedelta(minutes=10), "WORKING")),
            sink,
            _drawn(tmp_path),
        ).handle(ModelJoinJ16Command(write=True))
        assert _state(sink)[0] != "golden_only"

    def test_j16_control_without_a_run_the_fold_reads_not_run(self) -> None:
        assert _state(InMemoryEmissionSink()) == ("not_run", [])


class TestRuntime:
    def _env(self, tmp_path: Path, ledger_row: str | None) -> dict[str, str]:
        root = tmp_path / "overlays" / "node_join_j16_effect"
        root.mkdir(parents=True)
        (root / "overlay.yaml").write_text(
            EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8"
        )
        home = tmp_path / "home"
        (home / ".claude_scratch/factory-floor").mkdir(parents=True)
        (home / ".claude_scratch/factory-floor/floor-simple.json").write_text(
            json.dumps({"pieces": [{"piece": 16, "reading_age_s": 30}]}),
            encoding="utf-8",
        )
        ledger = tmp_path / "LEDGER.md"
        ledger.write_text((ledger_row or "") + "\n", encoding="utf-8")
        return {
            "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "overlays"),
            "EXAMPLE_FLOOR_HOME": str(home),
            "EXAMPLE_LEDGER_PATH": str(ledger),
        }

    def test_j16_run_writes_the_journal_a_probe_does_not(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        stamp = datetime.now(UTC) - timedelta(minutes=5)
        env = self._env(tmp_path, _reading(stamp, "WORKING"))
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        journal = (
            Path(env["EXAMPLE_FLOOR_HOME"]) / ".onex_state/floor-simple/emissions.jsonl"
        )
        assert main(["probe"]) == 0
        assert not journal.exists()
        assert main(["run"]) == 0
        assert "J16 piece 16 to 17: golden" in capsys.readouterr().out
        events = JsonlEmissionSink(journal).read(datetime(2000, 1, 1, tzinfo=UTC))
        assert [(e.piece, e.phase) for e in events] == [
            (16, "start"),
            (16, "success"),
            (17, "start"),
            (17, "success"),
        ]

    def test_j16_a_missing_home_is_indeterminate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = self._env(tmp_path, None)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        monkeypatch.delenv("EXAMPLE_FLOOR_HOME")
        assert main(["run"]) == 3
