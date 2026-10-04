# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""parity --utc-day D --receipt prints one STATUS row for UTC day D (OMN-20536 AC1).

The row is the daily parity receipt the work-ledger cutover's phase 1 exits on: it
carries file_rows, projection_rows, missing, extra, state_mismatches, unexplained and
backfilled for that day, and exact=yes only when every count is zero, backfilled is
0 and the day held at least one row. backfilled is a projection fact: 1 when any of
the day's projected rows came from the emit backfill tool (its own source value).
"""

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
ROWS = [
    "2026-10-02T23:59:59Z | STATUS | lane=a | the day before",
    "2026-10-03T00:00:00Z | STATUS | lane=a | first second of the day",
    "2026-10-03T12:00:00Z | STATUS | lane=b | midday",
    "2026-10-03T23:59:59Z | STATUS | lane=c | last second of the day",
    "2026-10-04T00:00:00Z | STATUS | lane=c | the next day",
]


def _rid(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cells(row: str) -> dict[str, str]:
    cells = [c.strip() for c in row.split(" | ")]
    return dict(c.split("=", 1) for c in cells[2:] if "=" in c and " " not in c)


def _run(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    projected: list[tuple[str, str]],
    extra_args: list[str] | None = None,
) -> tuple[int, str]:
    ledger = tmp_path / "ROLLING_WORK_LEDGER.md"
    ledger.write_text("# ledger\n" + "\n".join(ROWS) + "\n")
    export = tmp_path / "projection.json"
    export.write_text(
        json.dumps(
            {
                "rows": [
                    {"row_id": _rid(raw), "row_ts": raw[:20], "source": source}
                    for raw, source in projected
                ],
                "state": [],
            }
        )
    )
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    rc = main(
        [
            "--ledger", str(ledger), "--utc-day", DAY, "--projection-json", str(export),
            "--state-dir", str(state), "--receipt", *(extra_args or []),
        ]
    )  # fmt: skip
    return rc, capsys.readouterr().out


def test_utc_day_receipt_counts_a_day_with_one_missing_row(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, out = _run(
        tmp_path,
        capsys,
        [(ROWS[1], "onex-ledger"), (ROWS[2], "onex-ledger")],
    )

    assert rc == 1
    [row] = out.splitlines()
    assert " | STATUS | lane=work-ledger-parity | " in row
    cells = _cells(row)
    assert cells["day"] == DAY
    assert cells["actor"] == "script:work-ledger-parity"
    assert (cells["file_rows"], cells["projection_rows"]) == ("3", "2")
    assert (cells["missing"], cells["extra"], cells["state_mismatches"]) == (
        "1",
        "0",
        "0",
    )
    assert cells["unexplained"] == "1"
    assert cells["backfilled"] == "0"
    assert cells["exact"] == "no"


def test_utc_day_receipt_exact_day(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, out = _run(tmp_path, capsys, [(r, "onex-ledger") for r in ROWS[1:4]])

    assert rc == 0
    cells = _cells(out.strip())
    assert (cells["missing"], cells["extra"], cells["unexplained"]) == ("0", "0", "0")
    assert (cells["backfilled"], cells["exact"]) == ("0", "yes")


def test_utc_day_receipt_a_backfilled_day_is_never_exact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, out = _run(
        tmp_path,
        capsys,
        [
            (ROWS[1], "onex-ledger"),
            (ROWS[2], BACKFILL_SOURCE),
            (ROWS[3], "onex-ledger"),
        ],
    )

    cells = _cells(out.strip())
    assert cells["missing"] == "0"
    assert (cells["backfilled"], cells["exact"]) == ("1", "no")
    assert rc == 1


def test_utc_day_receipt_needs_a_day(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = tmp_path / "ROLLING_WORK_LEDGER.md"
    ledger.write_text(ROWS[1] + "\n")
    export = tmp_path / "projection.json"
    export.write_text(json.dumps({"rows": [], "state": []}))
    rc = main(
        [
            "--ledger", str(ledger), "--since", "2026-10-03T00:00:00Z",
            "--projection-json", str(export), "--receipt", "--state-dir", str(tmp_path),
        ]
    )  # fmt: skip
    assert rc == 2
    assert "--utc-day" in capsys.readouterr().err


def test_utc_day_window_is_the_whole_day_and_nothing_else(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = tmp_path / "ROLLING_WORK_LEDGER.md"
    ledger.write_text("\n".join(ROWS) + "\n")
    export = tmp_path / "projection.json"
    export.write_text(json.dumps({"rows": [], "state": []}))
    main(["--ledger", str(ledger), "--utc-day", DAY, "--projection-json", str(export)])
    report = json.loads(capsys.readouterr().out)
    assert report["file_rows"] == 3
    assert report["window_since"].startswith("2026-10-03T00:00:00")
    assert report["window_until"].startswith("2026-10-03T23:59:59")
