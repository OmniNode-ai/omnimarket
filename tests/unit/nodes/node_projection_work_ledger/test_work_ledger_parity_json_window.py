# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""JSON exports and database reads use the same inclusive parity window."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_projection_work_ledger.parity import (
    BACKFILL_SOURCE,
    main,
)

pytestmark = pytest.mark.unit

DAY = "2026-10-03"
STAMPS = (
    "2026-10-02T23:59:59Z",
    "2026-10-03T00:00:00Z",
    "2026-10-03T12:00:00Z",
    "2026-10-03T23:59:59Z",
    "2026-10-04T00:00:00Z",
)
ROWS = [f"{stamp} | STATUS | lane=window | measurement" for stamp in STAMPS]


def _row(raw: str, source: str = "onex-ledger") -> dict[str, str]:
    return {
        "row_id": hashlib.sha256(raw.encode()).hexdigest(),
        "row_ts": raw[:20],
        "source": source,
        "raw_row": raw,
    }


def _args(tmp_path: Path, rows: list[dict[str, str]]) -> list[str]:
    ledger = tmp_path / "ledger.md"
    ledger.write_text("\n".join(ROWS) + "\n")
    export = tmp_path / "projection.json"
    export.write_text(json.dumps({"rows": rows, "state": []}))
    return [
        "--ledger",
        str(ledger),
        "--projection-json",
        str(export),
        "--utc-day",
        DAY,
        "--state-dir",
        str(tmp_path),
    ]


def _cells(row: str) -> dict[str, str]:
    return dict(
        cell.split("=", 1)
        for cell in row.strip().split(" | ")[2:]
        if "=" in cell and " " not in cell
    )


def test_json_filters_both_outside_boundaries_and_keeps_both_endpoints(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(_args(tmp_path, [_row(raw) for raw in ROWS])) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["file_rows"] == report["projection_rows"] == 3
    assert report["mismatches"] == []


def test_json_explicit_window_has_the_same_inclusive_filter(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _args(tmp_path, [_row(raw) for raw in ROWS])
    day_index = args.index("--utc-day")
    args[day_index : day_index + 2] = ["--since", STAMPS[1], "--until", STAMPS[2]]
    assert main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["file_rows"] == report["projection_rows"] == 2
    assert report["mismatches"] == []


def test_json_in_window_extra_is_still_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    extra = _row("2026-10-03T13:00:00Z | STATUS | lane=extra | only in projection")
    assert main(_args(tmp_path, [_row(raw) for raw in ROWS] + [extra])) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["projection_rows"] == 4
    assert [(row["kind"], row["key"]) for row in report["mismatches"]] == [
        ("row_missing_in_file", extra["row_id"])
    ]


@pytest.mark.parametrize("backfill_index", [0, 1, 2, 3, 4])
def test_backfill_only_blocks_receipt_when_in_window(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backfill_index: int
) -> None:
    rows = [_row(raw) for raw in ROWS]
    rows[backfill_index]["source"] = BACKFILL_SOURCE
    in_window = backfill_index in (1, 2, 3)
    assert main([*_args(tmp_path, rows), "--receipt"]) == int(in_window)
    cells = _cells(capsys.readouterr().out)
    assert cells["file_rows"] == cells["projection_rows"] == "3"
    assert cells["missing"] == cells["extra"] == cells["state_mismatches"] == "0"
    assert cells["backfilled"] == str(int(in_window))
    assert cells["exact"] == ("no" if in_window else "yes")


@pytest.mark.parametrize(
    "stamp",
    ["not-a-stamp", "2026-10-02T23:59:59Zjunk", "2026-10-03T12:00:00Zjunk"],
)
@pytest.mark.parametrize("receipt", [False, True])
def test_malformed_export_timestamp_emits_no_report_or_receipt(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    stamp: str,
    receipt: bool,
) -> None:
    rows = [_row(raw) for raw in ROWS[1:4]]
    rows.append({"row_id": "invalid", "row_ts": stamp, "source": "onex-ledger"})
    args = _args(tmp_path, rows)
    if receipt:
        args.append("--receipt")
    assert main(args) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "work_ledger_parity: error:" in captured.err
