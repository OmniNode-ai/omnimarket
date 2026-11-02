# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The parity receipt names each of the 11 row kinds as exact or not (OMN-21885 AC1).

``exact_kinds=`` lists every kind once, ``KIND:yes`` when the kind had at least one row in
the day and zero missing, extra and text mismatches, else ``KIND:no``. State mismatches are
an entity fact, not a row fact, so they never move a kind.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.nodes.node_projection_work_ledger.parity import (
    compare,
    main,
    receipt_row,
)
from omnimarket.nodes.node_projection_work_ledger.parity_receipt import ROW_KINDS

pytestmark = pytest.mark.unit

DAY = "2026-10-27"
SINCE = datetime(2026, 10, 27, 0, 0, 0, tzinfo=UTC)
UNTIL = datetime(2026, 10, 27, 23, 59, 59, tzinfo=UTC)
NOW = datetime(2026, 11, 2, 0, 5, 0, tzinfo=UTC)

DAY_ROWS: dict[str, str] = {
    "CLAIM": "2026-10-27T10:00:00Z | CLAIM | lane=alpha | ticket=OMN-1 | repo=omnimarket | est ~1 lane-hours; displaces x; (OMN-1) | work",
    "TERMINAL": "2026-10-27T10:30:00Z | TERMINAL | lane=alpha | ticket=OMN-1 | friction=none | done",
    "HOLD": "2026-10-27T10:10:00Z | HOLD | lane=beta | id=2026-10-27T10:10:00Z-beta | surface=lab-dev | until=2026-10-27T12:00:00Z | reserved",
    "RELEASE": "2026-10-27T11:00:00Z | RELEASE | lane=beta | re=2026-10-27T10:10:00Z-beta | surface=lab-dev | result=PASS | restored=yes | done",
    "MSG": "2026-10-27T10:15:00Z | MSG | from=alpha | to=beta,gamma | id=2026-10-27T10:15:00Z-alpha | look",
    "ACK": "2026-10-27T10:16:00Z | ACK | from=beta | to=alpha | id=2026-10-27T10:16:00Z-beta | re=2026-10-27T10:15:00Z-alpha",
    "RULING": '2026-10-27T09:00:00Z | RULING | lane=orch | ticket=OMN-1 | "go" | text',
    "OPERATOR-CONSENT": '2026-10-27T09:01:00Z | OPERATOR-CONSENT | lane=orch | "yes" | APPROVED SCOPE: lab | OUT OF SCOPE: prod | evidence',
    "STATUS": "2026-10-27T10:05:00Z | STATUS | lane=alpha | ticket=OMN-1 | progress",
    "FRICTION": "2026-10-27T10:20:00Z | FRICTION | lane=alpha-1 | ticket=OMN-2 | cost=20m | class=tooling | the hook was slow",
    "CORRECTION": "2026-10-27T10:40:00Z | CORRECTION | lane=orch-1 | corrects=2026-10-27T10:05:00Z | wrong ticket",
}


def _rid(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cells(row: str) -> dict[str, str]:
    cells = [c.strip() for c in row.split(" | ")]
    return dict(c.split("=", 1) for c in cells[2:] if "=" in c and " " not in c)


def _kinds(row: str) -> dict[str, str]:
    value = _cells(row)["exact_kinds"]
    return dict(item.split(":", 1) for item in value.split(","))


def _receipt(
    file_rows: list[str],
    projected: dict[str, str],
    projected_text: dict[str, str] | None = None,
) -> str:
    """The receipt for DAY: ``projected`` is row_id -> row_ts, ``projected_text`` row_id -> text."""
    report = compare(
        file_rows=file_rows,
        projection_rows=projected,
        projection_state={},
        since=SINCE,
        until=UNTIL,
        projection_raw_rows=projected_text
        if projected_text is not None
        else {rid: raw for raw, rid in ((r, _rid(r)) for r in file_rows)},
    )
    return receipt_row(report, DAY, NOW)


def _projection_of(rows: list[str]) -> dict[str, str]:
    return {_rid(raw): raw[:20] for raw in rows}


def test_by_kind_receipt_names_all_eleven_kinds() -> None:
    assert len(ROW_KINDS) == 11
    assert set(ROW_KINDS) == set(DAY_ROWS)
    rows = list(DAY_ROWS.values())

    kinds = _kinds(_receipt(rows, _projection_of(rows)))

    assert tuple(kinds) == ROW_KINDS
    assert set(kinds.values()) == {"yes"}


def test_by_kind_a_row_missing_from_the_projection_marks_only_its_kind() -> None:
    rows = list(DAY_ROWS.values())
    projected = _projection_of([r for r in rows if "| STATUS |" not in r])

    kinds = _kinds(_receipt(rows, projected))

    assert kinds["STATUS"] == "no"
    assert {k for k, v in kinds.items() if v == "yes"} == set(ROW_KINDS) - {"STATUS"}


def test_by_kind_an_extra_projected_row_marks_the_kind_it_carries() -> None:
    rows = list(DAY_ROWS.values())
    stray = "2026-10-27T13:00:00Z | FRICTION | lane=zeta | ticket=OMN-9 | cost=5m | class=tooling | only in the db"
    text = {_rid(r): r for r in rows} | {_rid(stray): stray}
    projected = _projection_of(rows) | {_rid(stray): stray[:20]}

    kinds = _kinds(_receipt(rows, projected, text))

    assert kinds["FRICTION"] == "no"
    assert {k for k, v in kinds.items() if v == "yes"} == set(ROW_KINDS) - {"FRICTION"}


def test_by_kind_an_extra_projected_row_of_unknown_type_marks_every_kind() -> None:
    rows = list(DAY_ROWS.values())
    projected = _projection_of(rows) | {"f" * 64: "2026-10-27T13:00:00Z"}

    kinds = _kinds(_receipt(rows, projected))

    assert set(kinds.values()) == {"no"}


def test_by_kind_a_text_mismatch_marks_the_kind_of_the_row() -> None:
    rows = list(DAY_ROWS.values())
    hold = DAY_ROWS["HOLD"]
    text = {_rid(r): r for r in rows} | {_rid(hold): hold + " edited"}

    kinds = _kinds(_receipt(rows, _projection_of(rows), text))

    assert kinds["HOLD"] == "no"
    assert {k for k, v in kinds.items() if v == "yes"} == set(ROW_KINDS) - {"HOLD"}


def test_by_kind_a_kind_with_no_row_in_the_day_is_not_exact() -> None:
    rows = [r for k, r in DAY_ROWS.items() if k != "ACK"]

    kinds = _kinds(_receipt(rows, _projection_of(rows)))

    assert kinds["ACK"] == "no"
    assert {k for k, v in kinds.items() if v == "yes"} == set(ROW_KINDS) - {"ACK"}


def test_by_kind_a_state_mismatch_does_not_move_a_kind() -> None:
    rows = list(DAY_ROWS.values())
    report = compare(
        file_rows=rows,
        projection_rows=_projection_of(rows),
        projection_state={},
        since=SINCE,
        until=UNTIL,
        projection_raw_rows={_rid(r): r for r in rows},
    )
    row = receipt_row(report, DAY, NOW)

    assert int(_cells(row)["state_mismatches"]) > 0
    assert set(_kinds(row).values()) == {"yes"}


def test_by_kind_cli_receipt_carries_exact_kinds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = list(DAY_ROWS.values())
    ledger = tmp_path / "ROLLING_WORK_LEDGER.md"
    ledger.write_text("# ledger\n" + "\n".join(sorted(rows)) + "\n")
    export = tmp_path / "projection.json"
    export.write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "row_id": _rid(r),
                        "row_ts": r[:20],
                        "source": "onex-ledger",
                        "raw_row": r,
                    }
                    for r in rows
                    if "| FRICTION |" not in r
                ],
                "state": [],
            }
        )
    )
    state = tmp_path / "state"
    state.mkdir()

    rc = main(
        [
            "--ledger", str(ledger), "--utc-day", DAY, "--projection-json", str(export),
            "--state-dir", str(state), "--receipt",
        ]
    )  # fmt: skip

    [row] = capsys.readouterr().out.splitlines()
    kinds = _kinds(row)
    assert rc == 1
    assert kinds["FRICTION"] == "no"
    assert kinds["STATUS"] == "yes"
    assert tuple(kinds) == ROW_KINDS


def test_by_kind_23_missing_status_rows_name_status_and_nothing_else() -> None:
    """The shape of the plan's M6 measurement for 2026-10-27: 23 STATUS rows missing, the
    other ten kinds exact. A fixture of that shape, not the live day (that is the lab step)."""
    statuses = [
        f"2026-10-27T12:{i // 60:02d}:{i % 60:02d}Z | STATUS | lane=landing-x{i} | ticket=OMN-1 | tick {i}"
        for i in range(40)
    ]
    others = [r for k, r in DAY_ROWS.items() if k != "STATUS"]
    rows = statuses + others
    projected = _projection_of(statuses[23:] + others)

    row = _receipt(rows, projected)

    kinds = _kinds(row)
    assert _cells(row)["missing"] == "23"
    assert _cells(row)["exact"] == "no"
    assert kinds["STATUS"] == "no"
    assert {k for k, v in kinds.items() if v == "yes"} == set(ROW_KINDS) - {"STATUS"}
