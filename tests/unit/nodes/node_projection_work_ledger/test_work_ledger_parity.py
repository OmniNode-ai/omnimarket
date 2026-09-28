# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The parity check reports zero mismatches on a faithful projection and names a lost row."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_projection_work_ledger.handlers.work_ledger_fold import (
    apply_ops,
    fold_row,
)
from omnimarket.nodes.node_projection_work_ledger.models.model_work_ledger_fold_request import (
    ModelWorkLedgerFoldRequest,
)
from omnimarket.nodes.node_projection_work_ledger.models.model_work_ledger_parity_report import (
    EnumParityMismatchKind,
)
from omnimarket.nodes.node_projection_work_ledger.parity import (
    compare,
    main,
    split_rows,
)

pytestmark = pytest.mark.unit

LEDGER = """# Rolling ledger

## section
2026-09-28T10:00:00Z | CLAIM | lane=alpha | ticket=OMN-1 | est ~1 lane-hours; displaces x; (OMN-1) | work
2026-09-28T10:05:00Z | NOTE | lane=alpha | a legacy row the emit path can never carry
2026-09-28T10:30:00Z | TERMINAL | lane=alpha | ticket=OMN-1 | friction=none | done
2026-09-28T10:31:00Z | MSG | from=alpha | to=beta | id=2026-09-28T10:31:00Z-alpha | a message
  with a continuation line
2026-09-28T09:00:00Z | CLAIM | lane=old | ticket=OMN-0 | est ~1 lane-hours; displaces x; (OMN-0) | before the window
"""
SINCE = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)
UNTIL = datetime(2026, 9, 28, 23, 0, tzinfo=UTC)


def _project(rows: list[str]) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    log: dict[str, str] = {}
    state: dict[str, dict[str, object]] = {}
    for raw in rows:
        result = fold_row(ModelWorkLedgerFoldRequest(raw_row=raw))
        log[result.row.row_id] = f"{result.row.row_ts:%Y-%m-%dT%H:%M:%SZ}"
        apply_ops(state, result.ops)
    return log, {k: dict(v) for k, v in state.items()}


def _canonical(rows: list[str]) -> list[str]:
    return [
        r for r in rows if "| NOTE |" not in r and not r.startswith("2026-09-28T09")
    ]


def test_split_rows_groups_continuation_lines() -> None:
    rows = split_rows(LEDGER)
    assert len(rows) == 5
    assert rows[3].endswith("with a continuation line")


def test_a_faithful_projection_has_zero_mismatches() -> None:
    file_rows = split_rows(LEDGER)
    log, state = _project(_canonical(file_rows))
    report = compare(
        file_rows=file_rows,
        projection_rows=log,
        projection_state=state,
        since=SINCE,
        until=UNTIL,
    )
    assert report.mismatches == ()
    assert report.exact is True
    assert report.file_rows == 3
    assert report.unemittable_rows == 1
    assert report.unemittable_types == {"NOTE": 1}


def test_a_row_deleted_from_the_projection_is_named() -> None:
    file_rows = split_rows(LEDGER)
    log, state = _project(_canonical(file_rows))
    lost = next(k for k in log if k)
    del log[lost]
    report = compare(
        file_rows=file_rows,
        projection_rows=log,
        projection_state=state,
        since=SINCE,
        until=UNTIL,
    )
    assert report.exact is False
    assert (EnumParityMismatchKind.ROW_MISSING_IN_PROJECTION, lost) in [
        (m.kind, m.key) for m in report.mismatches
    ]


def test_a_row_only_in_the_projection_is_named() -> None:
    file_rows = split_rows(LEDGER)
    log, state = _project(_canonical(file_rows))
    log["f" * 64] = "2026-09-28T12:00:00Z"
    report = compare(
        file_rows=file_rows,
        projection_rows=log,
        projection_state=state,
        since=SINCE,
        until=UNTIL,
    )
    assert [m.kind for m in report.mismatches] == [
        EnumParityMismatchKind.ROW_MISSING_IN_FILE
    ]


def test_a_wrong_close_time_is_a_state_mismatch() -> None:
    file_rows = split_rows(LEDGER)
    log, state = _project(_canonical(file_rows))
    state["claim:alpha"]["closed_at"] = datetime(2026, 9, 28, 10, 45, tzinfo=UTC)
    report = compare(
        file_rows=file_rows,
        projection_rows=log,
        projection_state=state,
        since=SINCE,
        until=UNTIL,
    )
    assert [m.kind for m in report.mismatches] == [
        EnumParityMismatchKind.STATE_CLOSED_AT_DIFFERS
    ]


def test_an_empty_window_is_not_parity() -> None:
    report = compare(
        file_rows=[], projection_rows={}, projection_state={}, since=SINCE, until=UNTIL
    )
    assert report.exact is False


def test_cli_exit_codes_and_typed_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = tmp_path / "ROLLING_WORK_LEDGER.md"
    ledger.write_text(LEDGER)
    file_rows = split_rows(LEDGER)
    log, state = _project(_canonical(file_rows))
    export = tmp_path / "projection.json"
    export.write_text(
        json.dumps(
            {
                "rows": [{"row_id": k, "row_ts": v} for k, v in log.items()],
                "state": [
                    {
                        "entity_key": k,
                        "opened_at": v["opened_at"].isoformat()
                        if v["opened_at"]
                        else None,
                        "closed_at": v["closed_at"].isoformat()
                        if v["closed_at"]
                        else None,
                    }
                    for k, v in state.items()
                ],
            }
        )
    )
    args = [
        "--ledger", str(ledger), "--since", "2026-09-28T10:00:00Z",
        "--until", "2026-09-28T23:00:00Z", "--projection-json", str(export),
    ]  # fmt: skip
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["mismatches"] == []

    export.write_text(json.dumps({"rows": [], "state": []}))
    assert main([*args, "--format", "text"]) == 1
    assert "exact=False" in capsys.readouterr().out
    assert main(["--ledger", str(tmp_path / "missing.md"), *args[2:]]) == 2
