# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The J0 join run by the publish cycle (OMN-23022): golden and error chain event tests, and the failing controls.

J0 is piece 17 (the floor) to piece 1 (the code graph). The join ended at the floor's cycle node and only its golden half ever
ran: the board read it half run. The node writes piece 17's ``join_j0`` chain (a success when the floor record is fresh and draws
a reading age for the code graph, a typed failure otherwise) and, under its own id, the control's error chain: the same reader
fed the overlay's deliberately corrupt record, so piece 17 ends in a typed failure and piece 1 holds a typed ``upstream_`` refusal
with no start. The floor's own receipt fold then judges the join at the piece 17 node (``join_ends``).

Golden: a fresh record that draws piece 1 reads ``golden_only``; with the control's chains beside it the join reads ``passed``.
Error: an unreadable, non-JSON, stale or undrawn record is piece 17's typed failure and piece 1's refusal (``error_only``, no
finding). Failing controls: a control payload the reader accepts writes nothing and leaves the join ``golden_only`` and the run
exiting 1; the control's piece 17 failure dropped from the journal leaves the join not ``passed``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.emission_sink import (
    InMemoryEmissionSink,
    JsonlEmissionSink,
    ProtocolEmissionSink,
)
from omnimarket.models.floor_17a import (
    ModelFloorReceiptInput,
    ModelFloorReceipts,
    ModelJoinDef,
    ModelJoinEnd,
    ModelNodeEmission,
    ModelPieceDef,
)
from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
    HandlerFloorReceipt,
)
from omnimarket.nodes.node_join_j0_effect import HandlerJoinJ0
from omnimarket.nodes.node_join_j0_effect.handlers.handler_join_j0 import (
    FAIL_PIECE_NOT_DRAWN,
    FAIL_RECORD_STALE,
    FAIL_RECORD_UNREADABLE,
)
from omnimarket.nodes.node_join_j0_effect.models import (
    ModelJoinJ0Command,
    ModelJoinJ0Result,
)
from omnimarket.nodes.node_join_j0_effect.overlay import ModelJoinJ0Overlay
from omnimarket.nodes.node_join_j0_effect.runtime_join_j0 import main

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
EXAMPLE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_join_j0_effect/overlay.example.yaml"
)
FROM_NODE = "node_floor_simple_effect:join_j0"
RECORD_MAX_AGE_S = 900


def _overlay_data() -> dict[str, Any]:
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _overlay(control_text: str | None = None) -> ModelJoinJ0Overlay:
    data = _overlay_data()
    if control_text is not None:
        data["control"]["record_text"] = control_text
    return ModelJoinJ0Overlay.model_validate(data)


def _record(
    *,
    generated_at: datetime = NOW - timedelta(seconds=30),
    pieces: list[dict[str, object]] | None = None,
) -> str:
    rows = [{"piece": 1, "reading_age_s": 5}] if pieces is None else pieces
    return json.dumps(
        {
            "generated_at": generated_at.isoformat(),
            "freshness_max_age_s": RECORD_MAX_AGE_S,
            "pieces": rows,
        }
    )


def _file(tmp_path: Path, text: str | None) -> Path:
    path = tmp_path / "floor-simple.json"
    if text is not None:
        path.write_text(text, encoding="utf-8")
    return path


class _DropsControlFailure(InMemoryEmissionSink):
    """The failing control: a journal that loses piece 17's failure under the control's id."""

    def emit(self, event: ModelNodeEmission) -> None:
        if event.piece == 17 and event.phase == "failure":
            return
        super().emit(event)


def _handler(
    sink: ProtocolEmissionSink, record: Path, overlay: ModelJoinJ0Overlay | None = None
) -> HandlerJoinJ0:
    return HandlerJoinJ0(
        overlay=overlay or _overlay(),
        sink=sink,
        floor_record_path=record,
        backend="in_memory",
        clock=lambda: NOW,
    )


def _fold(
    events: tuple[ModelNodeEmission, ...], *, ends: bool = True
) -> ModelFloorReceipts:
    return HandlerFloorReceipt().handle(
        ModelFloorReceiptInput(
            now=NOW + timedelta(seconds=1),
            emissions=events,
            pieces=tuple(
                ModelPieceDef(piece=n, name=f"piece {n}", reading_max_age_s=86400)
                for n in range(1, 19)
            ),
            joins=(
                ModelJoinDef(
                    id="J0", from_piece=17, to_piece=1, label="step 0 to the code graph"
                ),
            ),
            join_ends=(ModelJoinEnd(join="J0", node=FROM_NODE),) if ends else (),
            freshness_max_age_s=7200,
            error_max_age_s=86400,
            chain_deadline_s=900,
            judged_to_pieces=(1, 17),
        )
    )


def _state(
    events: tuple[ModelNodeEmission, ...], *, ends: bool = True
) -> tuple[str, list[str]]:
    receipts = _fold(events, ends=ends)
    return receipts.joins[0].state, [f.code for f in receipts.findings]


def _run(
    record: Path,
    sink: ProtocolEmissionSink | None = None,
    *,
    overlay: ModelJoinJ0Overlay | None = None,
) -> tuple[ModelJoinJ0Result, ProtocolEmissionSink]:
    out = sink or InMemoryEmissionSink()
    return _handler(out, record, overlay).handle(ModelJoinJ0Command(write=True)), out


def _events(sink: ProtocolEmissionSink) -> tuple[ModelNodeEmission, ...]:
    return sink.read(NOW - timedelta(days=1))


def _chain(
    events: tuple[ModelNodeEmission, ...], cid: str
) -> list[tuple[int, str, str | None]]:
    return [
        (e.piece, e.phase, e.failure_code) for e in events if e.correlation_id == cid
    ]


class TestGoldenChain:
    def test_j0_golden_a_fresh_record_that_draws_the_code_graph_reads_golden(
        self, tmp_path: Path
    ) -> None:
        result, sink = _run(_file(tmp_path, _record()))
        events = _events(sink)
        assert (result.outcome, result.wrote, result.floor_rows) == ("golden", True, 1)
        assert _chain(events, result.correlation_id) == [
            (17, "start", None),
            (17, "success", None),
        ]
        assert {
            e.node for e in events if e.correlation_id == result.correlation_id
        } == {FROM_NODE}
        # The control's pair is beside it, so the join reads both halves; only the golden half is asserted above.
        assert result.control == "broke"

    def test_j0_golden_the_real_journal_file_backend_reads_the_same(
        self, tmp_path: Path
    ) -> None:
        journal = JsonlEmissionSink(tmp_path / "journal" / "emissions.jsonl")
        result, _ = _run(_file(tmp_path, _record()), journal)
        events = _events(journal)
        assert result.outcome == "golden"
        assert _state(events) == ("passed", [])

    def test_j0_golden_without_the_control_the_join_is_half_run(
        self, tmp_path: Path
    ) -> None:
        _, sink = _run(_file(tmp_path, _record()))
        golden_only = tuple(
            e for e in _events(sink) if not e.correlation_id.startswith("j0-control-")
        )
        assert _state(golden_only) == ("golden_only", [])

    def test_j0_golden_the_control_pair_is_the_error_chain_and_the_join_passes(
        self, tmp_path: Path
    ) -> None:
        result, sink = _run(_file(tmp_path, _record()))
        events = _events(sink)
        assert result.control == "broke"
        assert result.control_correlation_id.startswith("j0-control-")
        control = _chain(events, result.control_correlation_id)
        assert control == [
            (17, "start", None),
            (17, "failure", FAIL_RECORD_UNREADABLE),
            (1, "failure", f"upstream_{FAIL_RECORD_UNREADABLE}"),
        ]
        assert _state(events) == ("passed", [])

    def test_j0_golden_probe_writes_nothing_to_the_real_sink(
        self, tmp_path: Path
    ) -> None:
        sink = InMemoryEmissionSink()
        result = _handler(sink, _file(tmp_path, _record())).handle(
            ModelJoinJ0Command(write=False)
        )
        assert (result.outcome, result.wrote, result.control) == (
            "golden",
            False,
            "broke",
        )
        assert sink.events == []


class TestErrorChains:
    @pytest.mark.parametrize(
        ("text", "code"),
        [
            (None, FAIL_RECORD_UNREADABLE),
            ("{ not json", FAIL_RECORD_UNREADABLE),
            ("[]", FAIL_RECORD_UNREADABLE),
            (
                _record(generated_at=NOW - timedelta(seconds=RECORD_MAX_AGE_S + 60)),
                FAIL_RECORD_STALE,
            ),
            (_record(pieces=[{"piece": 2, "reading_age_s": 5}]), FAIL_PIECE_NOT_DRAWN),
            (_record(pieces=[{"piece": 1}]), FAIL_PIECE_NOT_DRAWN),
            (
                _record(pieces=[{"piece": 1, "reading_age_s": True}]),
                FAIL_PIECE_NOT_DRAWN,
            ),
        ],
    )
    def test_j0_error_a_record_that_cannot_draw_the_code_graph_is_piece_17s_typed_failure(
        self, tmp_path: Path, text: str | None, code: str
    ) -> None:
        result, sink = _run(_file(tmp_path, text))
        events = _events(sink)
        assert (result.outcome, result.failure_code, result.control) == (
            "error",
            code,
            "not_run",
        )
        assert _chain(events, result.correlation_id) == [
            (17, "start", None),
            (17, "failure", code),
            (1, "failure", f"upstream_{code}"),
        ]
        assert _state(events) == ("error_only", [])


class TestFailingControls:
    def test_j0_control_a_corrupt_payload_the_reader_accepts_writes_nothing_and_exits_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        accepted = _record()
        result, sink = _run(_file(tmp_path, accepted), overlay=_overlay(accepted))
        events = _events(sink)
        assert (result.outcome, result.control) == ("golden", "did_not_break")
        assert not any(e.correlation_id.startswith("j0-control-") for e in events)
        assert _state(events) == ("golden_only", [])

        # main() reads the wall clock, so the record the runtime accepts must be fresh against it, not against NOW.
        accepted = _record(generated_at=datetime.now(UTC))
        data = _overlay_data()
        data["control"]["record_text"] = accepted
        root = tmp_path / "overlays" / "node_join_j0_effect"
        root.mkdir(parents=True)
        (root / "overlay.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
        home = tmp_path / "home"
        record = home / data["emission"]["floor_record_relpath"]
        record.parent.mkdir(parents=True)
        record.write_text(accepted, encoding="utf-8")
        monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "overlays"))
        monkeypatch.setenv(data["emission"]["home_env"], str(home))
        # The runtime also needs the real floor contract to name the node; the example overlay does.
        assert main(["run"]) == 1

    def test_j0_control_the_control_failure_dropped_from_the_journal_is_not_passed(
        self, tmp_path: Path
    ) -> None:
        sink = _DropsControlFailure()
        _run(_file(tmp_path, _record()), sink)
        state, _ = _state(tuple(sink.events))
        assert state != "passed"

    def test_j0_control_without_the_join_end_the_floor_reads_the_cycle_again(
        self, tmp_path: Path
    ) -> None:
        _, sink = _run(_file(tmp_path, _record()))
        # No join end: the join reads the pair of chains under one id, as every other join does, so the golden chain
        # (piece 17 succeeded and piece 1 never answered) is not a golden reading any more.
        state, _ = _state(_events(sink), ends=False)
        assert state != "passed"


class TestRuntime:
    def _root(
        self, tmp_path: Path, mutate: dict[str, object] | None = None
    ) -> dict[str, Any]:
        data = _overlay_data()
        for key, value in (mutate or {}).items():
            data["join"][key] = value
        root = tmp_path / "overlays" / "node_join_j0_effect"
        root.mkdir(parents=True)
        (root / "overlay.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
        return data

    def _env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, data: dict[str, Any]
    ) -> Path:
        home = tmp_path / "home"
        record = home / data["emission"]["floor_record_relpath"]
        record.parent.mkdir(parents=True)
        record.write_text(_record(generated_at=datetime.now(UTC)), encoding="utf-8")
        monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "overlays"))
        monkeypatch.setenv(data["emission"]["home_env"], str(home))
        return home / data["emission"]["journal_relpath"]

    def test_j0_runtime_run_writes_the_chains_to_the_journal_file(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        journal = self._env(monkeypatch, tmp_path, self._root(tmp_path))
        assert main(["run"]) == 0
        assert "J0 piece 17 to 1: golden" in capsys.readouterr().out
        assert len(journal.read_text(encoding="utf-8").splitlines()) == 5

    def test_j0_runtime_probe_writes_no_journal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        journal = self._env(monkeypatch, tmp_path, self._root(tmp_path))
        assert main(["probe"]) == 0
        assert not journal.exists()

    @pytest.mark.parametrize(
        "mutate",
        [
            {"from_node": "node_floor_simple_effect:not_a_floor_chain"},
            {"join": "J5"},
        ],
    )
    def test_j0_runtime_a_node_the_floor_does_not_judge_is_indeterminate(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        mutate: dict[str, object],
    ) -> None:
        self._env(monkeypatch, tmp_path, self._root(tmp_path, mutate))
        assert main(["run"]) == 3
        assert "INDETERMINATE" in capsys.readouterr().err

    def test_j0_runtime_a_missing_overlay_is_indeterminate(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "nothing"))
        assert main(["run"]) == 3
        assert "INDETERMINATE" in capsys.readouterr().err
