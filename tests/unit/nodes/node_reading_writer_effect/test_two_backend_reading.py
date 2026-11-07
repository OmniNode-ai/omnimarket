# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Piece 4's reading row carries the count of effects not yet on both backends (OMN-22911)."""

from __future__ import annotations

import pytest

from omnimarket.models.model_reading import (
    EnumReadingVerdict,
    ModelReadingWindowCommand,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.ledger_reads import reading_rows
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
    evidence_row_from_text,
)
from omnimarket.nodes.node_reading_writer_effect.handlers.handler_reading_writer import (
    HandlerReadingWriter,
)
from omnimarket.nodes.node_reading_writer_effect.overlay import ModelReadingOverlay
from omnimarket.nodes.node_two_backend_suite_effect.adapters.report_sources import (
    InMemoryReportSource,
)
from omnimarket.nodes.node_two_backend_suite_effect.models.model_two_backend_suite import (
    ModelEffectBackendEntry,
    ModelEffectBackendReport,
)
from tests.nodes.node_floor_simple_effect.test_ledger_readings_floor_simple import (
    overlay as floor_overlay,
)
from tests.nodes.node_reading_writer_effect.reading_support import (
    RUN_AT,
    START,
    overlay_document,
    stamp,
)

pytestmark = pytest.mark.unit

PIECE = 4


def entry(name: str, both: bool) -> ModelEffectBackendEntry:
    return ModelEffectBackendEntry(
        node_name=name,
        path=f"{name}/contract.yaml",
        has_in_memory_default=both,
        has_real_backend_overlay=both,
    )


def report(*pairs: tuple[str, bool]) -> ModelEffectBackendReport:
    return ModelEffectBackendReport(entries=tuple(entry(n, b) for n, b in pairs))


def overlay_with_piece_4(with_setting: bool = True) -> ModelReadingOverlay:
    document = overlay_document()
    document["order"] = [4, *document["order"]]
    document["pieces"] = [
        {
            "piece": PIECE,
            "name": "contracts overlays injection",
            "readable_today": True,
            "basis": "two-backend-suite-status",
            "sources": [
                {
                    "source_id": "suite",
                    "row_type": "STATUS",
                    "lane": "two-backend-suite",
                    "mode": "newest",
                    "pass_when": [{"cell": "red", "op": "eq", "value": "0"}],
                    "max_age_seconds": 3600,
                }
            ],
            "injected_fault": "a planted effect that passes on one backend only",
        },
        *document["pieces"],
    ]
    if with_setting:
        document["two_backend"] = {
            "piece": PIECE,
            "report_path_env": "TWO_BACKEND_REPORT",
        }
    return ModelReadingOverlay.model_validate(document)


def suite_row(red: int) -> str:
    return (
        f"{stamp(START.replace(minute=30))} | STATUS | lane=two-backend-suite | principal=dev-test"
        f" | actor=node:node_two_backend_suite_effect | model=none | red={red}"
        f" | req=s{red} | via=bus:h200 | two-backend suite run"
    )


def write(report_in, *, red: int = 0, setting: bool = True):
    ledger = InMemoryLedger([suite_row(red)])
    writer = HandlerReadingWriter(
        overlay=overlay_with_piece_4(setting),
        source=ledger,
        appender=ledger,
        clock=lambda: RUN_AT,
        two_backend_source=report_in,
    )
    result = writer.handle(
        ModelReadingWindowCommand(
            correlation_id="c-1", window_start=START, pieces=(PIECE,)
        )
    )
    rows = [
        line
        for line in ledger.lines
        if "src=reading" in line and f"piece={PIECE}" in line
    ]
    return result, rows


def cells(row: str) -> dict[str, str]:
    parsed = evidence_row_from_text(row)
    assert parsed is not None
    return parsed.cells


def test_two_backend_pending_count_is_a_cell_of_the_piece_4_row() -> None:
    result, rows = write(
        InMemoryReportSource(report(("a", True), ("b", False), ("c", False)))
    )
    assert result.pieces[0].verdict is EnumReadingVerdict.WORKING
    assert len(rows) == 1
    assert cells(rows[0])["two_backend_pending"] == "2"
    assert "two-backend-pending-2" in cells(rows[0])["reason"]


def test_two_backend_floor_shows_the_count() -> None:
    _, rows = write(InMemoryReportSource(report(("a", False), ("b", False))))
    rule = floor_overlay().ledger_readings
    assert rule is not None
    shown, rejected = reading_rows([evidence_row_from_text(r) for r in rows], rule)
    assert rejected == 0
    assert "two-backend-pending-2" in shown[0].reason


def test_two_backend_unreadable_report_reads_unread_not_zero() -> None:
    class Broken:
        def read(self):
            from omnimarket.nodes.node_two_backend_suite_effect.protocols import (
                ModelReportRead,
            )

            return ModelReportRead(error="report-unreadable:FileNotFoundError")

    _, rows = write(Broken())
    assert cells(rows[0])["two_backend_pending"] == "unread"
    assert "two-backend-unread" in cells(rows[0])["reason"]


def test_two_backend_red_suite_row_turns_the_reading_not_working() -> None:
    green, _ = write(InMemoryReportSource(report(("a", True))), red=0)
    red, _ = write(InMemoryReportSource(report(("a", True))), red=1)
    assert green.pieces[0].verdict is EnumReadingVerdict.WORKING
    assert red.pieces[0].verdict is EnumReadingVerdict.NOT_WORKING


def test_two_backend_no_setting_means_no_cell() -> None:
    _, rows = write(InMemoryReportSource(report(("a", False))), setting=False)
    assert "two_backend_pending" not in cells(rows[0])
