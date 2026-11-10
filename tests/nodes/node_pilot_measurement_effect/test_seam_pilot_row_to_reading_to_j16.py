# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seams of the piece 16 pilot measurement, each read from both sides (OMN-23611).

Seam 1, pilots to the measurement: a pilot reports one STATUS row (src=pilot-result) with its bar hash, its stamps and one
``m_<metric>`` cell per metric; the measurement reads it. A cell name takes no dot (the ledger grammar's field key is
``[A-Za-z][A-Za-z0-9_-]*``), which is why the metric cells are ``m_<metric>``.
Seam 2, the measurement to the reading writer: the measurement's row (src=pilot-measurement) is the row piece 16's source in the
reading writer's overlay selects (the source below is spelled here as the overlay declares it; the overlays repository pins the
overlay itself). Anything but WORKING fails that source, so an UNKNOWN row reads NOT_WORKING.
Seam 3, the reading writer to join J16 (piece 16 to piece 17, lane p17chain-2730 owns piece 17's side): the writer's reading row
of piece 16 is the row the J16 node reads; a reading of any verdict is an eval result that reaches the floor, a missing reading is
piece 16's typed failure.

Each seam is driven end to end on the in-memory ledger; the failing control of each is a change on one side that the other side
turns into a named, different result.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.models.model_pilot_measurement import ModelPilotFact
from omnimarket.models.model_reading import (
    EnumCellOp,
    EnumReadingVerdict,
    EnumSourceMode,
    ModelCellPredicate,
    ModelPieceReadingSpec,
    ModelReadingComputeRequest,
    ModelReadingSource,
    ModelReadingSourceRows,
)
from omnimarket.nodes.node_join_j16_effect import HandlerJoinJ16
from omnimarket.nodes.node_join_j16_effect.handlers.handler_join_j16 import (
    FAIL_NO_READING,
)
from omnimarket.nodes.node_join_j16_effect.models import ModelJoinJ16Command
from omnimarket.nodes.node_join_j16_effect.overlay import ModelJoinJ16Overlay
from omnimarket.nodes.node_pilot_manifest_check_compute.handlers.handler_pilot_manifest_check import (
    compute_bar_hash,
)
from omnimarket.nodes.node_pilot_measurement_effect.adapters.adapter_ledger_pilot_facts import (
    LedgerPilotFacts,
)
from omnimarket.nodes.node_pilot_measurement_effect.handlers.handler_pilot_measurement_run import (
    HandlerPilotMeasurementRun,
)
from omnimarket.nodes.node_pilot_measurement_effect.models.model_pilot_measurement_run import (
    ModelPilotMeasurementCommand,
)
from omnimarket.nodes.node_pilot_measurement_effect.overlay import (
    ModelPilotMeasurementOverlay,
)
from omnimarket.nodes.node_reading_compute.handlers.handler_reading_compute import (
    HandlerReadingCompute,
    stamp_text,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    FileLedgerRowSource,
    InMemoryLedger,
    evidence_row_from_text,
)
from omnimarket.nodes.node_reading_writer_effect.handlers.handler_reading_writer import (
    build_reading_row,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes"
NOW = datetime(2026, 11, 10, 18, 0, tzinfo=UTC)
PILOT = "example-memory"


def _overlay() -> ModelPilotMeasurementOverlay:
    text = (ROOT / "node_pilot_measurement_effect/overlay.example.yaml").read_text(
        encoding="utf-8"
    )
    return ModelPilotMeasurementOverlay.model_validate(yaml.safe_load(text))


def _stamp(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def _bar_hash(overlay: ModelPilotMeasurementOverlay, pilot: str = PILOT) -> str:
    declaration = next(p for p in overlay.pilots if p.pilot_id == pilot)
    return compute_bar_hash(declaration.bar, overlay.manifest_check.hash_algorithm)


def _pilot_row(
    overlay: ModelPilotMeasurementOverlay,
    *,
    bar_hash: str | None = "own",
    finished: bool = True,
    metrics: dict[str, float] | None = None,
) -> str:
    """The row a pilot reports: the shape the measurement documents for the pilots' lanes."""
    started = NOW - timedelta(hours=1)
    cells = [
        "lane=memory-pilot",
        "src=pilot-result",
        f"pilot={PILOT}",
        f"bar_hash={_bar_hash(overlay) if bar_hash == 'own' else (bar_hash or 'none')}",
        f"frozen_at={_stamp(started - timedelta(hours=1))}",
        f"started_at={_stamp(started)}",
        f"finished_at={_stamp(NOW - timedelta(minutes=5)) if finished else 'none'}",
    ]
    if finished:
        values = {"accuracy_gain": 0.2, "planted_score": 6.0, "ingest_hours": 1.0}
        values.update(metrics or {})
        cells += [f"m_{name}={value}" for name, value in values.items()]
    return f"{_stamp(NOW - timedelta(minutes=5))} | STATUS | " + " | ".join(cells)


def _measure(ledger: InMemoryLedger, overlay: ModelPilotMeasurementOverlay) -> str:
    """Run the measurement once over the ledger's pilot rows and return the row it appended."""
    handler = HandlerPilotMeasurementRun(
        overlay=overlay,
        source=LedgerPilotFacts(overlay, ledger),
        appender=ledger,
        clock=lambda: NOW,
        host="seam-host",
    )
    result = handler.handle(
        ModelPilotMeasurementCommand(correlation_id="seam", write=True)
    )
    assert result.status == "completed", result.failure
    assert result.row is not None
    return result.row


def _piece_16_spec(overlay: ModelPilotMeasurementOverlay) -> ModelPieceReadingSpec:
    """Piece 16's source as the reading writer's overlay declares it (lane and cells are the measurement's own publish lane)."""
    return ModelPieceReadingSpec(
        piece=16,
        name="pilots and eval",
        readable_today=True,
        basis="hourly-pilot-measurement-row",
        sources=(
            ModelReadingSource(
                source_id="pilot_measurement",
                row_type="STATUS",
                lane=overlay.publish.lane,
                cells={"src": "pilot-measurement", "drill": "no"},
                mode=EnumSourceMode.NEWEST,
                pass_when=(
                    ModelCellPredicate(cell="verdict", op=EnumCellOp.EQ, value="WORKING"),
                ),
                max_age_seconds=7200,
            ),
        ),
        injected_fault="A pilot starts without a frozen bar, or the planted fact is not recalled",
    )


def _read_piece_16(
    overlay: ModelPilotMeasurementOverlay, measurement_rows: list[str]
) -> tuple[EnumReadingVerdict, tuple[str, ...], str]:
    """The reading writer's piece 16 reading from the measurement's rows, and the reading row it would append."""
    rows = tuple(
        row
        for line in measurement_rows
        if (row := evidence_row_from_text(line)) is not None
    )
    start = NOW.replace(minute=0)
    computed = HandlerReadingCompute().handle(
        ModelReadingComputeRequest(
            spec=_piece_16_spec(overlay),
            window_start=start - timedelta(hours=1),
            window_end=start,
            cutoff=start + timedelta(minutes=10),
            order_hash="o" * 12,
            overlay_hash="h" * 12,
            revision="seam",
            sources=(
                ModelReadingSourceRows(source_id="pilot_measurement", rows=rows),
            ),
        )
    )
    text = build_reading_row(
        computed,
        lane="reading-writer",
        actor="node:node_reading_writer_effect",
        published_at=NOW,
        probed_at=NOW,
        max_age_seconds=7200,
    )
    return computed.verdict, computed.reasons, text


def _j16(tmp_path: Path, reading_rows: list[str], drawn: bool = True) -> Any:
    ledger = tmp_path / "LEDGER.md"
    ledger.write_text("\n".join(reading_rows) + "\n", encoding="utf-8")
    record = tmp_path / "floor-simple.json"
    record.write_text(
        '{"pieces": [{"piece": 16, "reading_age_s": 0}]}' if drawn else '{"pieces": []}',
        encoding="utf-8",
    )
    example = (ROOT / "node_join_j16_effect/overlay.example.yaml").read_text(
        encoding="utf-8"
    )
    overlay = ModelJoinJ16Overlay.model_validate(yaml.safe_load(example))
    return HandlerJoinJ16(
        overlay=overlay,
        source=FileLedgerRowSource(ledger, None),
        sink=InMemoryEmissionSink(),
        floor_record_path=record,
        backend="in_memory",
        clock=lambda: NOW,
    ).handle(ModelJoinJ16Command(write=True))


def test_seam_1_a_pilot_row_with_its_declared_bar_and_a_good_result_reads_working() -> (
    None
):
    overlay = _overlay()
    ledger = InMemoryLedger([_pilot_row(overlay)])
    row = _measure(ledger, overlay)
    assert " | verdict=WORKING | " in row
    assert " | accepted=example-memory | " in row


@pytest.mark.parametrize(
    ("row_kwargs", "reason"),
    [
        ({"bar_hash": None}, "pilot-refused:example-memory:hash_missing"),
        ({"bar_hash": "f" * 64}, "pilot-refused:example-memory:hash_mismatch"),
        (
            {"metrics": {"planted_score": 3.0}},
            "pilot-bar-missed:example-memory:planted_score",
        ),
    ],
)
def test_seam_1_control_a_pilot_row_that_breaks_its_bar_reads_not_working(
    row_kwargs: dict[str, Any], reason: str
) -> None:
    overlay = _overlay()
    ledger = InMemoryLedger([_pilot_row(overlay, **row_kwargs)])
    row = _measure(ledger, overlay)
    assert " | verdict=NOT_WORKING | " in row
    assert f"reason={reason}" in row


def test_seam_1_control_a_metric_cell_with_a_dot_never_reaches_the_measurement() -> (
    None
):
    """The ledger grammar drops a cell name with a dot: a pilot that reports ``m.planted_score`` loses it and the result is
    read as a missing metric, never as a pass."""
    overlay = _overlay()
    line = _pilot_row(overlay).replace("m_planted_score=", "m.planted_score=")
    row = _measure(InMemoryLedger([line]), overlay)
    assert " | verdict=NOT_WORKING | " in row
    assert "planted_score" in row


def test_seam_2_the_measurement_row_is_the_row_piece_16_reads_and_working_passes_it() -> (
    None
):
    overlay = _overlay()
    row = _measure(InMemoryLedger([_pilot_row(overlay)]), overlay)
    verdict, reasons, _text = _read_piece_16(overlay, [row])
    assert (verdict, reasons) == (EnumReadingVerdict.WORKING, ())


def test_seam_2_an_unknown_measurement_fails_the_source_and_reads_not_working() -> None:
    overlay = _overlay()
    row = _measure(InMemoryLedger([]), overlay)
    assert " | verdict=UNKNOWN | " in row
    assert "reason=no-pilot-started" in row
    verdict, reasons, _text = _read_piece_16(overlay, [row])
    assert verdict is EnumReadingVerdict.NOT_WORKING
    assert reasons == ("source-fail:pilot_measurement",)


def test_seam_2_control_a_drill_row_is_never_read_as_the_reading() -> None:
    overlay = _overlay()
    ledger = InMemoryLedger([_pilot_row(overlay)])
    good = _measure(ledger, overlay)
    drill = _drill_row(overlay, ledger)
    assert " | drill=yes | " in drill
    # the drill row is the newest row, yet the reading still reads the operational row
    verdict, _reasons, _text = _read_piece_16(overlay, [good, drill])
    assert verdict is EnumReadingVerdict.WORKING


def _drill_row(overlay: ModelPilotMeasurementOverlay, ledger: InMemoryLedger) -> str:
    handler = HandlerPilotMeasurementRun(
        overlay=overlay,
        source=LedgerPilotFacts(overlay, ledger),
        appender=InMemoryLedger(),
        clock=lambda: NOW + timedelta(minutes=1),
        host="seam-host",
        drill=True,
    )
    result = handler.handle(
        ModelPilotMeasurementCommand(correlation_id="seam-drill", write=True)
    )
    assert result.row is not None
    return result.row


def test_seam_2_control_a_stale_measurement_reads_not_working() -> None:
    overlay = _overlay()
    row = _measure(InMemoryLedger([_pilot_row(overlay)]), overlay)
    start = NOW.replace(minute=0)
    stale = row.replace(stamp_text(NOW), stamp_text(start - timedelta(hours=5)), 1)
    verdict, reasons, _text = _read_piece_16(overlay, [stale])
    assert verdict is EnumReadingVerdict.NOT_WORKING
    assert reasons[0].startswith("stale-source:pilot_measurement")


def test_seam_3_j16_reads_the_reading_row_of_piece_16_and_draws_it(
    tmp_path: Path,
) -> None:
    overlay = _overlay()
    row = _measure(InMemoryLedger([_pilot_row(overlay)]), overlay)
    _verdict, _reasons, reading = _read_piece_16(overlay, [row])
    result = _j16(tmp_path, [reading])
    assert (result.outcome, result.failure_code) == ("golden", "")


def test_seam_3_a_not_working_reading_is_still_an_eval_result_for_j16(
    tmp_path: Path,
) -> None:
    overlay = _overlay()
    row = _measure(InMemoryLedger([]), overlay)
    _verdict, _reasons, reading = _read_piece_16(overlay, [row])
    assert "verdict=NOT_WORKING" in reading
    assert _j16(tmp_path, [reading]).outcome == "golden"


def test_seam_3_control_no_reading_row_is_piece_16s_typed_failure(
    tmp_path: Path,
) -> None:
    result = _j16(tmp_path, [])
    assert (result.outcome, result.failed_side, result.failure_code) == (
        "error",
        "from",
        FAIL_NO_READING,
    )


def test_seam_3_control_a_reading_the_floor_record_does_not_draw_fails_piece_17(
    tmp_path: Path,
) -> None:
    overlay = _overlay()
    row = _measure(InMemoryLedger([_pilot_row(overlay)]), overlay)
    _verdict, _reasons, reading = _read_piece_16(overlay, [row])
    result = _j16(tmp_path, [reading], drawn=False)
    assert (result.outcome, result.failed_side) == ("error", "to")


def test_the_pilot_fact_a_row_carries_is_the_models_fact() -> None:
    overlay = _overlay()
    ledger = InMemoryLedger([_pilot_row(overlay)])
    facts = LedgerPilotFacts(overlay, ledger).read(NOW)
    assert facts.unread_reason is None
    [fact] = facts.facts
    assert isinstance(fact, ModelPilotFact)
    assert fact.pilot_id == PILOT
    assert fact.results is not None
    assert fact.results["planted_score"] == 6.0
