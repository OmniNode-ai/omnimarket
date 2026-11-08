# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The fill's owned and running counts equal the lanes that wrote in the window (OMN-22888).

The snapshot replay feeds the ledger as read at 2026-11-07T10:36:35Z through the node and compares its count with an
independent CLAIM and TERMINAL liveness count taken straight from the snapshot lines. Every chain test reads the
chain back through the floor's own receipt fold. Each error test has a control beside it: the same read without the
injected break must read golden, which shows the failure assertions can fail.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    NodeOverlayNotFoundError,
)
from omnimarket.models.floor_17a import (
    ModelFloorReceiptInput,
    ModelFloorReceipts,
    ModelPieceDef,
)
from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
    HandlerFloorReceipt,
)
from omnimarket.nodes.node_floor_fill_owner_count_compute.handlers.handler_floor_fill_owner_count import (
    FAILURE_SILENT,
    FAILURE_UNPARSEABLE,
    FAILURE_UNREADABLE,
    NODE_NAME,
    FloorFillOwnerCountError,
    HandlerFloorFillOwnerCount,
    load_overlay,
)
from omnimarket.nodes.node_floor_fill_owner_count_compute.models import (
    ModelFloorFillOwnerCountOverlay,
    ModelFloorFillOwnerCountRequest,
    ModelFloorFillOwnerCountResult,
)
from omnimarket.nodes.node_floor_fill_owner_count_compute.protocols.protocol_lane_write_reader import (
    EnumLaneWriteBreak,
    InMemoryLaneWriteReader,
    ProtocolLaneWriteReader,
)
from tests.node_overlay_support import install_node_overlay

pytestmark = pytest.mark.unit

SNAPSHOT = Path(__file__).parent / "ledger_snapshot_2026-11-07T1036Z.txt"
SNAPSHOT_AT = datetime(2026, 11, 7, 10, 36, 35, tzinfo=UTC)
PIECE = 3
OVERLAY_FIELDS = {
    "lane_write_window_minutes": 120,
    "liveness_kinds": ["CLAIM", "TERMINAL"],
    "emission_piece": PIECE,
}
# The count of the replayed snapshot, read once from the snapshot lines by ``_independent_count`` below.
SNAPSHOT_LANES_120 = 69


def _overlay(window: int = 120) -> ModelFloorFillOwnerCountOverlay:
    return ModelFloorFillOwnerCountOverlay.model_validate(
        {**OVERLAY_FIELDS, "lane_write_window_minutes": window}
    )


def _independent_count(text: str, at: datetime, minutes: int) -> set[str]:
    """CLAIM and TERMINAL liveness taken straight from the lines, sharing no code with the node."""
    start = at - timedelta(minutes=minutes)
    lanes: set[str] = set()
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        stamp, kind, cell = (part.strip() for part in line.split("|", 2))
        when = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        if start <= when <= at and kind in ("CLAIM", "TERMINAL") and "lane=" in cell:
            lanes.add(cell.split("lane=", 1)[1].split()[0])
    return lanes


def _clock() -> Callable[[], datetime]:
    state = {"n": 0}

    def tick() -> datetime:
        state["n"] += 1
        return SNAPSHOT_AT + timedelta(seconds=state["n"])

    return tick


def _handler(
    reader: ProtocolLaneWriteReader,
    sink: InMemoryEmissionSink,
    overlay: ModelFloorFillOwnerCountOverlay | None = None,
) -> HandlerFloorFillOwnerCount:
    return HandlerFloorFillOwnerCount(overlay or _overlay(), reader, _clock(), sink)


def _fold(sink: InMemoryEmissionSink) -> ModelFloorReceipts:
    return HandlerFloorReceipt().handle(
        ModelFloorReceiptInput(
            now=SNAPSHOT_AT + timedelta(minutes=5),
            emissions=tuple(sink.events),
            pieces=(
                ModelPieceDef(piece=PIECE, name="owner-count", reading_max_age_s=900),
            ),
            joins=(),
            freshness_max_age_s=900,
            error_max_age_s=900,
            chain_deadline_s=300,
        )
    )


def _replay(
    window: int = 120, sink: InMemoryEmissionSink | None = None
) -> ModelFloorFillOwnerCountResult:
    text = SNAPSHOT.read_text(encoding="utf-8")
    handler = _handler(
        InMemoryLaneWriteReader(text), sink or InMemoryEmissionSink(), _overlay(window)
    )
    return handler.handle(ModelFloorFillOwnerCountRequest(as_of=SNAPSHOT_AT))


# ------------------------------------------------------------------------------------------------------ snapshot replay


def test_snapshot_replay_running_count_equals_the_claim_and_terminal_liveness_count_over_120_minutes() -> (
    None
):
    text = SNAPSHOT.read_text(encoding="utf-8")
    expected = _independent_count(text, SNAPSHOT_AT, 120)

    result = _replay()

    assert set(result.lanes) == expected
    assert result.running_count == len(expected) == SNAPSHOT_LANES_120
    assert result.owned_count == result.running_count
    assert result.window_minutes == 120


def test_snapshot_replay_the_window_is_the_overlay_setting_not_a_constant() -> None:
    text = SNAPSHOT.read_text(encoding="utf-8")
    narrow = _replay(window=30)
    wide = _replay(window=240)

    assert narrow.running_count == len(_independent_count(text, SNAPSHOT_AT, 30))
    assert wide.running_count == len(_independent_count(text, SNAPSHOT_AT, 240))
    assert narrow.running_count < _replay().running_count < wide.running_count


def test_snapshot_replay_a_lane_that_wrote_only_outside_the_window_is_not_counted() -> (
    None
):
    # A control for the count: a lane whose last CLAIM is older than the window is in the snapshot but not counted.
    text = SNAPSHOT.read_text(encoding="utf-8")
    old_only = {
        lane
        for lane in _independent_count(text, SNAPSHOT_AT, 240)
        if lane not in _independent_count(text, SNAPSHOT_AT, 120)
    }
    assert old_only
    assert old_only.isdisjoint(_replay().lanes)


def test_snapshot_replay_a_status_row_is_not_a_liveness_write() -> None:
    text = SNAPSHOT.read_text(encoding="utf-8")
    status_only = {
        line.split("lane=", 1)[1].split()[0]
        for line in text.splitlines()
        if " | STATUS | lane=" in line
    } - _independent_count(text, SNAPSHOT_AT, 240)
    assert status_only
    assert status_only.isdisjoint(_replay().lanes)


def test_a_window_with_rows_and_no_claim_or_terminal_is_zero_lanes_not_a_failure() -> (
    None
):
    # Positive control for the zero: the ledger answered, nobody wrote a liveness row.
    text = "2026-11-07T10:30:00Z | STATUS | lane=alpha\n2026-11-07T10:31:00Z | FRICTION | lane=beta\n"
    sink = InMemoryEmissionSink()
    result = _handler(InMemoryLaneWriteReader(text), sink).handle(
        ModelFloorFillOwnerCountRequest(as_of=SNAPSHOT_AT)
    )
    assert result.running_count == result.owned_count == 0
    assert result.lines_read == 2
    assert [e.phase for e in sink.events] == ["start", "success"]


# ------------------------------------------------------------------------------------------------------------ golden


def test_golden_chain_a_read_that_counts_emits_a_start_and_a_success_the_fold_counts_golden() -> (
    None
):
    sink = InMemoryEmissionSink()
    result = _replay(sink=sink)

    assert [e.phase for e in sink.events] == ["start", "success"]
    assert len({e.correlation_id for e in sink.events}) == 1
    assert {e.node for e in sink.events} == {NODE_NAME}
    assert {e.piece for e in sink.events} == {PIECE}
    assert {e.backend for e in sink.events} == {"in_memory"}
    assert sink.events[1].counters["lanes"] == result.running_count

    receipts = _fold(sink)
    assert receipts.findings == ()
    (chain,) = receipts.chains
    assert chain.kind == "golden"
    (row,) = receipts.pieces
    assert row.golden_at is not None
    assert row.error_at is None


# ------------------------------------------------------------------------------------------------------------- error


def test_error_chain_a_ledger_that_cannot_be_read_is_a_typed_failure_not_zero_lanes() -> (
    None
):
    sink = InMemoryEmissionSink()
    reader = InMemoryLaneWriteReader(
        SNAPSHOT.read_text(encoding="utf-8"), EnumLaneWriteBreak.LEDGER_UNREADABLE
    )
    with pytest.raises(FloorFillOwnerCountError) as raised:
        _handler(reader, sink).handle(
            ModelFloorFillOwnerCountRequest(as_of=SNAPSHOT_AT)
        )

    assert raised.value.code == FAILURE_UNREADABLE
    assert [e.phase for e in sink.events] == ["start", "failure"]
    assert sink.events[1].failure_code == FAILURE_UNREADABLE
    receipts = _fold(sink)
    (chain,) = receipts.chains
    assert chain.kind == "error"
    (row,) = receipts.pieces
    assert row.error_at is not None
    assert row.golden_at is None


def test_error_chain_a_silent_ledger_is_a_typed_failure_not_zero_lanes() -> None:
    sink = InMemoryEmissionSink()
    reader = InMemoryLaneWriteReader(
        SNAPSHOT.read_text(encoding="utf-8"), EnumLaneWriteBreak.LEDGER_SILENT
    )
    with pytest.raises(FloorFillOwnerCountError) as raised:
        _handler(reader, sink).handle(
            ModelFloorFillOwnerCountRequest(as_of=SNAPSHOT_AT)
        )

    assert raised.value.code == FAILURE_SILENT
    assert sink.events[-1].failure_code == FAILURE_SILENT
    (chain,) = _fold(sink).chains
    assert chain.kind == "error"


def test_error_chain_a_ledger_of_unparseable_lines_is_a_typed_failure() -> None:
    sink = InMemoryEmissionSink()
    with pytest.raises(FloorFillOwnerCountError) as raised:
        _handler(InMemoryLaneWriteReader("garbage\nmore garbage\n"), sink).handle(
            ModelFloorFillOwnerCountRequest(as_of=SNAPSHOT_AT)
        )
    assert raised.value.code == FAILURE_UNPARSEABLE
    assert sink.events[-1].failure_code == FAILURE_UNPARSEABLE


@pytest.mark.parametrize("injected", list(EnumLaneWriteBreak))
def test_error_chain_control_the_same_read_without_the_injected_break_is_golden(
    injected: EnumLaneWriteBreak,
) -> None:
    text = SNAPSHOT.read_text(encoding="utf-8")
    broken, clean = InMemoryEmissionSink(), InMemoryEmissionSink()
    with pytest.raises(FloorFillOwnerCountError):
        _handler(InMemoryLaneWriteReader(text, injected), broken).handle(
            ModelFloorFillOwnerCountRequest(as_of=SNAPSHOT_AT)
        )
    _handler(InMemoryLaneWriteReader(text), clean).handle(
        ModelFloorFillOwnerCountRequest(as_of=SNAPSHOT_AT)
    )

    assert [c.kind for c in _fold(broken).chains] == ["error"]
    assert [c.kind for c in _fold(clean).chains] == ["golden"]


# ------------------------------------------------------------------------------------------------------------ overlay


def test_overlay_the_window_comes_from_the_node_overlay_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_node_overlay(
        monkeypatch,
        tmp_path,
        NODE_NAME,
        {**OVERLAY_FIELDS, "lane_write_window_minutes": 45},
    )
    overlay = load_overlay()
    assert overlay.lane_write_window_minutes == 45
    result = _handler(
        InMemoryLaneWriteReader(SNAPSHOT.read_text(encoding="utf-8")),
        InMemoryEmissionSink(),
        overlay,
    ).handle(ModelFloorFillOwnerCountRequest(as_of=SNAPSHOT_AT))
    assert result.window_minutes == 45


def test_overlay_a_missing_overlay_refuses_naming_the_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    with pytest.raises(NodeOverlayNotFoundError, match="lane_write_window_minutes"):
        load_overlay()


def test_overlay_a_window_that_is_not_positive_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_node_overlay(
        monkeypatch,
        tmp_path,
        NODE_NAME,
        {**OVERLAY_FIELDS, "lane_write_window_minutes": 0},
    )
    with pytest.raises(NodeOverlayError, match="lane_write_window_minutes"):
        load_overlay()


def test_the_default_reader_is_in_memory_and_reads_nothing_so_it_fails_silent() -> None:
    sink = InMemoryEmissionSink()
    with pytest.raises(FloorFillOwnerCountError) as raised:
        HandlerFloorFillOwnerCount(_overlay(), sink=sink).handle(
            ModelFloorFillOwnerCountRequest(as_of=SNAPSHOT_AT)
        )
    assert raised.value.code == FAILURE_SILENT
