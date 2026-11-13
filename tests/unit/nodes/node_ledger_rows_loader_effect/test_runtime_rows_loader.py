# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The rows loader CLI end to end on the in-memory path (OMN-23977, cutover plan step L7).

Exit 0 with the node's rows; a seeded outage with the ledger file named is exit 10, the file's rows,
``source=file-fallback`` and a count that grows in the journal across runs; a retired fallback and an
outage with no file named are exit 69 with no rows.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_ledger_rows_loader_effect.runtime_rows_loader import main
from omnimarket.nodes.node_work_ledger_reader_effect.protocols import (
    ProtocolLedgerPointRead,
)
from tests.unit.nodes.node_ledger_rows_loader_effect.test_rows_loader_reader_seam import (
    OutagePointRead,
    ledger_file,
    query_node,
)
from tests.unit.nodes.node_work_ledger_reader_effect.test_reader_rows_window import (
    MSG_ROW,
    SEAM_NOW,
    STATUS_ROWS,
    QueryNodePointRead,
)

pytestmark = pytest.mark.unit

READER_OVERLAY = """\
moved_readers: [drain-map]
max_watermark_age_seconds: 900
page_size: 200
max_pages: 50
route: {kind: inmemory}
parity: {receipt_lane: work-ledger-parity, window_hours: 48}
events: {journal_env: ONEX_LEDGER_READER_JOURNAL}
"""
ROWS = (*STATUS_ROWS, MSG_ROW)


def env(tmp_path: Path, *, fallback: bool = True) -> dict[str, str]:
    (tmp_path / "reader.yaml").write_text(READER_OVERLAY)
    (tmp_path / "loader.yaml").write_text(
        f"file_fallback_allowed: {str(fallback).lower()}\n"
        "journal_env: ONEX_LEDGER_ROWS_LOADER_JOURNAL\n"
    )
    return {
        "ONEX_LEDGER_READER_OVERLAY": str(tmp_path / "reader.yaml"),
        "ONEX_LEDGER_ROWS_LOADER_OVERLAY": str(tmp_path / "loader.yaml"),
        "ONEX_LEDGER_ROWS_LOADER_JOURNAL": str(tmp_path / "journal.jsonl"),
    }


def run(
    tmp_path: Path,
    point_read: ProtocolLedgerPointRead,
    capsys: pytest.CaptureFixture[str],
    *extra: str,
    fallback: bool = True,
) -> tuple[int, str, str]:
    code = main(
        ["--reader", "drain-map", "--window-hours", "27", *extra],
        environ=env(tmp_path, fallback=fallback),
        point_read=point_read,
        clock=lambda: SEAM_NOW,
    )
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_the_node_answers_exit_zero_with_its_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run(tmp_path, QueryNodePointRead(query_node(ROWS)), capsys)
    assert code == 0
    assert out.splitlines() == list(ROWS)
    assert "source=node" in err
    assert "fallback_count=0" in err


def test_a_seeded_outage_reads_the_file_exit_ten_and_the_journal_counts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = ledger_file(tmp_path, ROWS)
    for expected in (1, 2):
        code, out, err = run(
            tmp_path, OutagePointRead(), capsys, "--file-ledger", str(ledger)
        )
        assert code == 10
        assert out.splitlines() == list(ROWS)
        assert "source=file-fallback" in err
        assert f"fallback_count={expected}" in err
    journal = (tmp_path / "journal.jsonl").read_text().splitlines()
    assert len(journal) == 2


def test_a_retired_fallback_is_exit_69_with_no_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = ledger_file(tmp_path, ROWS)
    code, out, err = run(
        tmp_path,
        OutagePointRead(),
        capsys,
        "--file-ledger",
        str(ledger),
        fallback=False,
    )
    assert (code, out) == (69, "")
    assert "reason=file-fallback-retired" in err


def test_an_outage_with_no_ledger_file_named_is_exit_69(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run(tmp_path, OutagePointRead(), capsys)
    assert (code, out) == (69, "")
    assert "reason=node-failed-and-no-file-source" in err


def test_the_archive_glob_adds_the_archive_splits_to_the_fallback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = ledger_file(tmp_path, ROWS)
    archive = tmp_path / "archive"
    archive.mkdir()
    archived = "2026-11-09T09:00:00Z | STATUS | lane=arch | archived"
    (archive / "ledger_2026-11-09-split.md").write_text(archived + "\n")
    args = ("--file-ledger", str(ledger), "--archive-glob", "ledger_*-split.md")
    code, out, _ = run(tmp_path, OutagePointRead(), capsys, *args)
    assert code == 10
    assert out.splitlines() == [archived, *ROWS]
    code, out, _ = run(
        tmp_path, OutagePointRead(), capsys, "--file-ledger", str(ledger)
    )
    assert out.splitlines() == list(ROWS)
