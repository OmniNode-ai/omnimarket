# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Join J3, bus to overlays (OMN-23084): the drift read answers the upstream piece's newest success, so the floor reads J3 golden.

The floor builds piece 3's chains from its seam-deadline ledger rows, one correlation id per row, and piece 4's from the
drift rows. Before this change the two never shared an id, so no run of the drift read could be the golden half of the join:
J3 could read at best error_only. Now a run takes the newest successful seam chain nobody has answered, names its id in the
``upstream_cid`` cell of the row it writes, and the floor reads that row as piece 4's chain under the same id.

Golden: the answered chain makes J3 golden, and with a typed refusal of a failed seam chain beside it the join reads
``passed``. Error: no upstream chain, a drill, an answered chain and an unreadable ledger claim no join. Failing control: the
same row without the cell leaves J3 not golden.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.models.floor_17a import (
    ModelFloorReceiptInput,
    ModelFloorReceipts,
    ModelJoinDef,
    ModelPieceDef,
    ModelPieceEventRule,
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
from omnimarket.nodes.node_overlay_drift_effect.handlers.handler_overlay_drift import (
    HandlerOverlayDrift,
)
from omnimarket.nodes.node_overlay_drift_effect.handlers.upstream_chain import (
    newest_unanswered_upstream_cid,
)
from omnimarket.nodes.node_overlay_drift_effect.models.model_overlay_drift import (
    ModelOverlayDriftCommand,
)
from omnimarket.nodes.node_overlay_drift_effect.overlay import (
    ModelOverlayDriftOverlay,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    FileLedgerRowSource,
    InMemoryLedger,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelLedgerQuery,
    ModelLedgerRead,
)
from tests.nodes.node_overlay_drift_effect.overlay_drift_support import (
    EXAMPLE,
    RUN_AT,
    World,
)

pytestmark = pytest.mark.unit

SEAM_LANE = "example-seam-deadline"
CID_CELL = "upstream_cid"
READ_AT = RUN_AT + timedelta(minutes=10)
REFUSAL_EXAMPLE = (
    EXAMPLE.parent.parent / "node_join_refusal_effect" / "overlay.example.yaml"
)

SEAM_RULE = {
    "piece": 3,
    "node": "example_seam_node:read",
    "golden": {
        "row_type": "STATUS",
        "lane": SEAM_LANE,
        "cells": {
            "src": SEAM_LANE,
            "drill": "no",
            "verdict": "WORKING",
            "outcome": "read",
        },
    },
    "error": {
        "row_type": "STATUS",
        "lane": SEAM_LANE,
        "cells": {"src": SEAM_LANE, "verdict": "NOT_WORKING"},
    },
    "failure_code_cell": "reason",
    "failure_default": "no_result_by_deadline",
    "error_drill_cell": "drill",
    "lookback_days": 7,
}
#: The floor's rule for the drift node's own rows, with the cell that names the chain a row answers.
DRIFT_RULE = ModelPieceEventRule.model_validate(
    {
        "piece": 4,
        "node": "example_overlay_node:read",
        "golden": {
            "row_type": "STATUS",
            "lane": "overlay-drift",
            "cells": {"src": "overlay-drift", "verdict": "WORKING", "outcome": "read"},
        },
        "error": {
            "row_type": "STATUS",
            "lane": "overlay-drift",
            "cells": {"src": "overlay-drift", "verdict": "NOT_WORKING"},
        },
        "failure_code_cell": "reason",
        "failure_default": "overlay_drift",
        "error_drill_cell": "drill",
        "lookback_days": 7,
        "correlation_cell": CID_CELL,
    }
)


def _drift_overlay() -> ModelOverlayDriftOverlay:
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["upstream"] = {
        "ledger_read": {
            "route": "file",
            "path_env": "EXAMPLE_LEDGER_PATH",
            "archive_subdir": "archive",
            "timeout_seconds": 60,
            "row_limit": 5000,
        },
        "rule": SEAM_RULE,
        "cid_cell": CID_CELL,
    }
    return ModelOverlayDriftOverlay.model_validate(document)


def _seam_row(at: datetime, verdict: str, *, drill: str = "no") -> str:
    stamp = at.strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f"{stamp} | STATUS | lane={SEAM_LANE} | src={SEAM_LANE} | verdict={verdict} "
        f"| outcome=read | reason=none | drill={drill} | probed_at={stamp}"
    )


def _write(path: Path, *rows: str) -> FileLedgerRowSource:
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return FileLedgerRowSource(path, None)


class _Unreadable:
    def read(self, query: ModelLedgerQuery) -> ModelLedgerRead:
        return ModelLedgerRead(error="ledger-file-unreadable:OSError")


def _seam_cid(rows: list[str], source: FileLedgerRowSource, *, win: bool) -> str:
    """The id the floor derives for the seam chain at the end of ``rows`` (the success, or the failure)."""
    read = source.read(
        ModelLedgerQuery(
            since=READ_AT - timedelta(days=7),
            until=READ_AT + timedelta(days=1),
            row_type="STATUS",
            lane=SEAM_LANE,
        )
    )
    rule = ModelPieceEventRule.model_validate(SEAM_RULE)
    phase = "success" if win else "failure"
    [event] = [e for e in piece_events(list(read.rows), (rule,)) if e.phase == phase]
    return event.correlation_id


def _run_drift(tmp_path: Path, upstream_cid: str | None) -> str:
    """One drift read as the node runs it; the row it appends."""
    world = World("memory", tmp_path)
    ledger = InMemoryLedger()
    HandlerOverlayDrift(
        overlay=_drift_overlay(),
        host="example-host",
        home=world.home(),
        validator=world.validator(),
        appender=ledger,
        clock=lambda: RUN_AT,
    ).handle(
        ModelOverlayDriftCommand(
            correlation_id="overlay-drift-example-host-run",
            write=True,
            upstream_cid=upstream_cid,
        )
    )
    [appended] = ledger.lines
    return appended.strip()


def _refusal_overlay() -> ModelJoinRefusalOverlay:
    data = yaml.safe_load(REFUSAL_EXAMPLE.read_text(encoding="utf-8"))
    join = data["joins"][0]
    join.update(
        {
            "join": "J3",
            "from_piece": 3,
            "to_piece": 4,
            "downstream_node": "example_overlay_node:join_j3",
        }
    )
    join["upstream"] = SEAM_RULE
    return ModelJoinRefusalOverlay.model_validate(data)


def _j3(source: FileLedgerRowSource, sink: InMemoryEmissionSink) -> ModelFloorReceipts:
    """The floor's fold of the ledger rows as piece events plus the journal, judged for J3."""
    rows = []
    for lane in (SEAM_LANE, "overlay-drift"):
        rows += list(
            source.read(
                ModelLedgerQuery(
                    since=READ_AT - timedelta(days=7),
                    until=READ_AT + timedelta(days=1),
                    row_type="STATUS",
                    lane=lane,
                )
            ).rows
        )
    rules = (ModelPieceEventRule.model_validate(SEAM_RULE), DRIFT_RULE)
    events = (*sink.read(READ_AT - timedelta(hours=48)), *piece_events(rows, rules))
    return HandlerFloorReceipt().handle(
        ModelFloorReceiptInput(
            now=READ_AT,
            emissions=events,
            pieces=tuple(
                ModelPieceDef(piece=n, name=f"piece {n}", reading_max_age_s=86400)
                for n in range(1, 19)
            ),
            joins=(
                ModelJoinDef(
                    id="J3", from_piece=3, to_piece=4, label="bus to overlays"
                ),
            ),
            freshness_max_age_s=7200,
            error_max_age_s=86400,
            chain_deadline_s=900,
            judged_to_pieces=(3, 4),
        )
    )


def _refuse(source: FileLedgerRowSource, sink: InMemoryEmissionSink) -> None:
    HandlerJoinRefusal(
        overlay=_refusal_overlay(),
        source=source,
        sink=sink,
        backend="in_memory",
        clock=lambda: READ_AT,
    ).handle(ModelJoinRefusalCommand(write=True))


FAILED = RUN_AT - timedelta(hours=5)
WON = RUN_AT - timedelta(hours=2)


class TestGoldenChain:
    def test_the_finder_names_the_id_the_floor_derives_for_the_newest_seam_success(
        self, tmp_path: Path
    ) -> None:
        rows = [_seam_row(FAILED, "NOT_WORKING"), _seam_row(WON, "WORKING")]
        source = _write(tmp_path / "ledger.md", *rows)
        found = newest_unanswered_upstream_cid(
            source, _drift_overlay().upstream, "overlay-drift", READ_AT
        )
        assert found == _seam_cid(rows, source, win=True)

    def test_the_answered_chain_makes_j3_golden_and_with_a_refusal_it_passes(
        self, tmp_path: Path
    ) -> None:
        rows = [_seam_row(FAILED, "NOT_WORKING"), _seam_row(WON, "WORKING")]
        ledger = tmp_path / "ledger.md"
        source = _write(ledger, *rows)
        cid = newest_unanswered_upstream_cid(
            source, _drift_overlay().upstream, "overlay-drift", READ_AT
        )
        drift_row = _run_drift(tmp_path, cid)
        assert f"| {CID_CELL}={cid} |" in drift_row
        source = _write(ledger, *rows, drift_row)
        sink = InMemoryEmissionSink()
        before = _j3(source, sink)
        assert before.joins[0].state == "broken"
        assert [f.code for f in before.findings] == ["downstream_missing_typed_failure"]
        _refuse(source, sink)
        after = _j3(source, sink)
        assert after.findings == ()
        assert after.joins[0].state == "passed"
        assert after.joins[0].golden_at is not None
        assert after.joins[0].error_at is not None

    def test_a_chain_is_answered_once(self, tmp_path: Path) -> None:
        rows = [_seam_row(WON, "WORKING")]
        ledger = tmp_path / "ledger.md"
        source = _write(ledger, *rows)
        upstream = _drift_overlay().upstream
        cid = newest_unanswered_upstream_cid(source, upstream, "overlay-drift", READ_AT)
        source = _write(ledger, *rows, _run_drift(tmp_path, cid))
        assert (
            newest_unanswered_upstream_cid(source, upstream, "overlay-drift", READ_AT)
            is None
        )


class TestErrorChain:
    def test_no_upstream_chain_a_drill_and_an_unreadable_ledger_claim_no_join(
        self, tmp_path: Path
    ) -> None:
        upstream = _drift_overlay().upstream
        empty = _write(tmp_path / "empty.md", _seam_row(FAILED, "NOT_WORKING"))
        assert (
            newest_unanswered_upstream_cid(empty, upstream, "overlay-drift", READ_AT)
            is None
        )
        old = _write(
            tmp_path / "old.md", _seam_row(READ_AT - timedelta(days=30), "WORKING")
        )
        assert (
            newest_unanswered_upstream_cid(old, upstream, "overlay-drift", READ_AT)
            is None
        )
        drill = _write(tmp_path / "drill.md", _seam_row(WON, "WORKING", drill="yes"))
        assert (
            newest_unanswered_upstream_cid(drill, upstream, "overlay-drift", READ_AT)
            is None
        )
        assert (
            newest_unanswered_upstream_cid(
                _Unreadable(), upstream, "overlay-drift", READ_AT
            )
            is None
        )

    def test_a_row_without_an_upstream_cid_carries_no_cell(
        self, tmp_path: Path
    ) -> None:
        assert f"{CID_CELL}=" not in _run_drift(tmp_path, None)


class TestFailingControl:
    def test_the_same_row_without_the_cell_leaves_j3_without_a_golden(
        self, tmp_path: Path
    ) -> None:
        rows = [_seam_row(FAILED, "NOT_WORKING"), _seam_row(WON, "WORKING")]
        source = _write(tmp_path / "ledger.md", *rows, _run_drift(tmp_path, None))
        sink = InMemoryEmissionSink()
        _refuse(source, sink)
        state = _j3(source, sink).joins[0]
        assert state.state == "error_only"
        assert state.golden_at is None
