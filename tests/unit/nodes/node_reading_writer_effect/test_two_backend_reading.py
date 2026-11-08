# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Piece 4's reading row carries the count of effects not yet on both backends (OMN-22911)."""

from __future__ import annotations

import pytest

from omnimarket.models.model_effect_backend_report import (
    ModelEffectBackendEntry,
    ModelEffectBackendReport,
)
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
            from omnimarket.models.model_effect_backend_report import ModelReportRead

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


def write_twice_on_one_ledger(first, second):
    """Two runs of the same closed window against one ledger, the second after the report changed."""
    ledger = InMemoryLedger([suite_row(0)])
    outcomes = []
    for report_in in (first, second):
        writer = HandlerReadingWriter(
            overlay=overlay_with_piece_4(True),
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
        outcomes.append(result.pieces[0])
    return outcomes


def test_two_backend_same_report_replayed_is_already_written() -> None:
    same = InMemoryReportSource(report(("a", False), ("b", False)))
    first, second = write_twice_on_one_ledger(same, same)
    assert first.ledger != "already-written"
    assert second.ledger == "already-written"


def test_two_backend_changed_report_is_not_silently_reused_as_already_written() -> None:
    """The pending count is part of the row's content identity: a retry with another count is a replay failure, not a reuse."""
    _, second = write_twice_on_one_ledger(
        InMemoryReportSource(report(("a", False), ("b", False))),
        InMemoryReportSource(report(("a", False), ("b", False), ("c", False))),
    )
    assert second.ledger != "already-written"
    assert second.outcome.value == "replay-failed"


def test_two_backend_incomplete_inventory_reads_unread_not_a_count() -> None:
    incomplete = ModelEffectBackendReport(
        entries=(entry("a", True),), unreadable=("node_x/contract.yaml",)
    )
    _, rows = write(InMemoryReportSource(incomplete))
    assert cells(rows[0])["two_backend_pending"] == "unread"


def test_reading_writer_does_not_import_the_suite_nodes_private_package() -> None:
    """The report and its source port are shared models: the reading writer takes them from shared ownership, not from the suite's models or protocols."""
    import ast
    from pathlib import Path

    import omnimarket.nodes.node_reading_writer_effect as writer_package

    offenders = []
    for path in Path(writer_package.__file__).parent.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            module = getattr(node, "module", None) or ""
            names = [a.name for a in getattr(node, "names", [])]
            suite = "node_two_backend_suite_effect"
            private = (f"{suite}.models", f"{suite}.protocols")
            if isinstance(node, (ast.Import, ast.ImportFrom)) and any(
                part.startswith(f"omnimarket.nodes.{private[0]}")
                or part.startswith(f"omnimarket.nodes.{private[1]}")
                for part in [module, *names]
            ):
                offenders.append(f"{path.name}: {module or names}")
    assert offenders == []
