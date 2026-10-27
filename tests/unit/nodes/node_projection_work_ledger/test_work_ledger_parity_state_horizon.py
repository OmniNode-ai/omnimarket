# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Historical row parity compares touched entities through the file's horizon."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers.work_ledger_fold import apply_ops, fold_row, parse_stamp
from omnimarket.models.model_work_ledger_fold_request import ModelWorkLedgerFoldRequest
from omnimarket.nodes.node_projection_work_ledger.models.model_work_ledger_parity_report import (
    EnumParityMismatchKind,
)
from omnimarket.nodes.node_projection_work_ledger.parity import (
    compare,
    file_horizon,
    load_projection_later_rows_json,
    main,
)

pytestmark = pytest.mark.unit

SINCE = datetime(2026, 10, 20, tzinfo=UTC)
UNTIL = datetime(2026, 10, 20, 23, 59, 59, tzinfo=UTC)
KEY = "claim:dod-closeout-sweep"
ROWS = [
    "2026-10-20T22:53:28Z | CLAIM | lane=dod-closeout-sweep | ticket=OMN-1 | work",
    "2026-10-20T23:00:00Z | TERMINAL | lane=dod-closeout-sweep | done",
    "2026-10-24T14:52:46Z | CLAIM | lane=dod-closeout-sweep | ticket=OMN-2 | more work",
    "2026-10-24T15:00:00Z | TERMINAL | lane=dod-closeout-sweep | done again",
]
HORIZON = parse_stamp(ROWS[-1].split(" | ", 1)[0])
LATER_ROWS = [
    "2026-10-25T00:00:00Z | CLAIM | lane=dod-closeout-sweep | ticket=OMN-3 | later work",
    "2026-10-25T01:00:00Z | TERMINAL | lane=dod-closeout-sweep | done later",
]


def _row_log(rows: list[str]) -> dict[str, str]:
    log: dict[str, str] = {}
    for raw in rows:
        result = fold_row(ModelWorkLedgerFoldRequest(raw_row=raw))
        log[result.row.row_id] = f"{result.row.row_ts:%Y-%m-%dT%H:%M:%SZ}"
    return log


def _project(rows: list[str]) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    log: dict[str, str] = {}
    state: dict[str, dict[str, object]] = {}
    for raw in rows:
        result = fold_row(ModelWorkLedgerFoldRequest(raw_row=raw))
        if SINCE <= result.row.row_ts <= UNTIL:
            log[result.row.row_id] = f"{result.row.row_ts:%Y-%m-%dT%H:%M:%SZ}"
        apply_ops(state, result.ops)
    return log, {key: dict(value) for key, value in state.items()}


def test_later_claim_and_terminal_are_folded_for_historical_parity() -> None:
    log, state = _project(ROWS)
    report = compare(
        projection_raw_rows={
            hashlib.sha256(raw.encode()).hexdigest(): raw for raw in ROWS
        },
        file_rows=ROWS,
        projection_rows=log,
        projection_state=state,
        since=SINCE,
        until=UNTIL,
    )
    assert report.mismatches == ()
    assert report.exact is True
    assert report.state_horizon == HORIZON
    assert report.state_after_horizon == ()
    assert report.file_rows == report.projection_rows == 2
    assert report.state_entities_compared == 1

    historical = compare(
        projection_raw_rows={
            hashlib.sha256(raw.encode()).hexdigest(): raw for raw in ROWS
        },
        file_rows=ROWS,
        projection_rows=log,
        projection_state=state,
        since=SINCE,
        until=UNTIL,
        state_horizon=UNTIL,
        later_projection_rows=_row_log(ROWS[2:]),
    )
    # Both current timestamps are beyond this explicit horizon (rule 4).
    assert historical.mismatches == ()
    assert historical.state_after_horizon == (KEY,)

    _, old_state = _project(ROWS[:2])
    stale = compare(
        projection_raw_rows={
            hashlib.sha256(raw.encode()).hexdigest(): raw for raw in ROWS
        },
        file_rows=ROWS,
        projection_rows=log,
        projection_state=old_state,
        since=SINCE,
        until=UNTIL,
    )
    assert [m.kind for m in stale.mismatches] == [
        EnumParityMismatchKind.STATE_OPENED_AT_DIFFERS,
        EnumParityMismatchKind.STATE_CLOSED_AT_DIFFERS,
    ]


@pytest.mark.parametrize(
    "closed_at",
    [datetime(2026, 10, 25, tzinfo=UTC), "2026-10-24T23:00:00-01:00"],
)
def test_projection_later_than_file_is_counted_apart(
    closed_at: datetime | str,
) -> None:
    later_rows = [
        "2026-10-25T00:00:00Z | TERMINAL | lane=dod-closeout-sweep | done later"
    ]
    log, state = _project([*ROWS, *later_rows])
    state[KEY]["closed_at"] = closed_at
    report = compare(
        projection_raw_rows={
            hashlib.sha256(raw.encode()).hexdigest(): raw for raw in ROWS
        },
        file_rows=ROWS,
        projection_rows=log,
        projection_state=state,
        since=SINCE,
        until=UNTIL,
        later_projection_rows=_row_log(later_rows),
    )
    assert report.state_after_horizon == (KEY,)
    assert report.mismatches == ()
    assert report.exact is True


@pytest.mark.parametrize(
    "evidence", ["missing_row", "wrong_timestamp", "none", "empty", "file_row"]
)
def test_later_close_without_matching_row_evidence_is_a_mismatch(evidence: str) -> None:
    log, state = _project([*ROWS, *LATER_ROWS[1:]])
    later_rows: dict[str, str] | None = _row_log(LATER_ROWS[1:])
    row_id = str(state[KEY]["closed_row_id"])
    if evidence == "missing_row":
        later_rows = _row_log(LATER_ROWS[:1])
    elif evidence == "wrong_timestamp":
        later_rows = {row_id: "2026-10-25T02:00:00Z"}
    elif evidence == "none":
        later_rows = None
    elif evidence == "empty":
        later_rows = {}
    else:
        # Even matching evidence cannot excuse a timestamp on a row the file folded.
        file_row_id = next(iter(_row_log(ROWS[-1:])))
        state[KEY]["closed_row_id"] = file_row_id
        later_rows = {file_row_id: "2026-10-25T01:00:00Z"}
    report = compare(
        projection_raw_rows={
            hashlib.sha256(raw.encode()).hexdigest(): raw for raw in ROWS
        },
        file_rows=ROWS,
        projection_rows=log,
        projection_state=state,
        since=SINCE,
        until=UNTIL,
        later_projection_rows=later_rows,
    )
    assert [(m.kind, m.key) for m in report.mismatches] == [
        (EnumParityMismatchKind.STATE_CLOSED_AT_DIFFERS, KEY)
    ]
    assert report.state_after_horizon == ()
    assert report.exact is False


@pytest.mark.parametrize(
    "closed_at",
    [
        datetime(2026, 10, 24, 14, 59, tzinfo=UTC),
        "2026-10-24T16:59:00+02:00",
        None,
    ],
)
def test_wrong_close_time_within_horizon_is_still_a_mismatch(
    closed_at: datetime | str | None,
) -> None:
    log, state = _project(ROWS)
    state[KEY]["closed_at"] = closed_at
    report = compare(
        projection_raw_rows={
            hashlib.sha256(raw.encode()).hexdigest(): raw for raw in ROWS
        },
        file_rows=ROWS,
        projection_rows=log,
        projection_state=state,
        since=SINCE,
        until=UNTIL,
    )
    assert [(m.kind, m.key) for m in report.mismatches] == [
        (EnumParityMismatchKind.STATE_CLOSED_AT_DIFFERS, KEY)
    ]
    assert report.state_after_horizon == ()
    assert report.exact is False


def test_missing_entity_is_still_a_mismatch() -> None:
    log, _ = _project(ROWS)
    report = compare(
        projection_raw_rows={
            hashlib.sha256(raw.encode()).hexdigest(): raw for raw in ROWS
        },
        file_rows=ROWS,
        projection_rows=log,
        projection_state={},
        since=SINCE,
        until=UNTIL,
    )
    assert [(m.kind, m.key) for m in report.mismatches] == [
        (EnumParityMismatchKind.STATE_MISSING_IN_PROJECTION, KEY)
    ]
    assert report.exact is False


def test_only_window_entities_are_compared_and_unemittable_counts_stay_in_window() -> (
    None
):
    rows = [
        "2026-10-19T12:00:00Z | CLAIM | lane=before | old work",
        *ROWS,
        "2026-10-24T16:00:00Z | CLAIM | lane=outside | unrelated work",
    ]
    log, state = _project(rows)
    del state["claim:before"]
    del state["claim:outside"]
    rows.extend(
        [
            "2026-10-20T23:30:00Z | NOTE | legacy in window",
            "2026-10-24T17:00:00Z | NOTE | legacy outside window",
        ]
    )
    report = compare(
        projection_raw_rows={
            hashlib.sha256(raw.encode()).hexdigest(): raw for raw in rows
        },
        file_rows=rows,
        projection_rows=log,
        projection_state=state,
        since=SINCE,
        until=UNTIL,
    )
    assert report.state_horizon == datetime(2026, 10, 24, 17, tzinfo=UTC)
    assert report.state_entities_compared == 1
    assert report.file_rows == report.projection_rows == 2
    assert report.unemittable_rows == 1
    assert report.unemittable_types == {"NOTE": 1}
    assert report.exact is True


def test_ack_after_window_advances_the_message_state() -> None:
    rows = [
        "2026-10-20T22:00:00Z | MSG | from=alpha | to=beta | id=message | a message",
        "2026-10-21T09:00:00Z | ACK | from=beta | re=message | received",
    ]
    log, state = _project(rows)
    report = compare(
        projection_raw_rows={
            hashlib.sha256(raw.encode()).hexdigest(): raw for raw in rows
        },
        file_rows=rows,
        projection_rows=log,
        projection_state=state,
        since=SINCE,
        until=UNTIL,
    )
    assert report.file_rows == report.projection_rows == 1
    assert report.state_entities_compared == 1
    assert report.state_after_horizon == ()
    assert report.exact is True


def test_default_horizon_uses_maximum_timestamp_in_file_order() -> None:
    rows = list(reversed(ROWS))
    assert file_horizon(rows, UNTIL) == HORIZON
    log, state = _project(rows)
    report = compare(
        projection_raw_rows={
            hashlib.sha256(raw.encode()).hexdigest(): raw for raw in rows
        },
        file_rows=rows,
        projection_rows=log,
        projection_state=state,
        since=SINCE,
        until=UNTIL,
    )
    assert report.state_horizon == HORIZON
    assert report.exact is True


def test_empty_file_defaults_horizon_to_until() -> None:
    assert file_horizon([], UNTIL) == UNTIL
    report = compare(
        projection_raw_rows={},
        file_rows=[],
        projection_rows={},
        projection_state={},
        since=SINCE,
        until=UNTIL,
    )
    assert report.state_horizon == UNTIL
    assert report.state_after_horizon == ()
    assert report.exact is False


def test_after_horizon_exclusion_applies_only_to_the_later_field() -> None:
    log, state = _project([*ROWS, *LATER_ROWS[1:]])
    state[KEY]["opened_at"] = SINCE
    report = compare(
        projection_raw_rows={
            hashlib.sha256(raw.encode()).hexdigest(): raw for raw in ROWS
        },
        file_rows=ROWS,
        projection_rows=log,
        projection_state=state,
        since=SINCE,
        until=UNTIL,
        later_projection_rows=_row_log(LATER_ROWS[1:]),
    )
    assert report.state_after_horizon == (KEY,)
    assert [m.kind for m in report.mismatches] == [
        EnumParityMismatchKind.STATE_OPENED_AT_DIFFERS
    ]
    assert report.exact is False


def test_after_horizon_keys_are_unique_and_sorted() -> None:
    rows = [*ROWS, *(row.replace("dod-closeout-sweep", "alpha") for row in ROWS)]
    later_rows = [
        *LATER_ROWS,
        *(row.replace("dod-closeout-sweep", "alpha") for row in LATER_ROWS),
    ]
    log, state = _project([*rows, *later_rows])
    report = compare(
        projection_raw_rows={
            hashlib.sha256(raw.encode()).hexdigest(): raw for raw in rows
        },
        file_rows=rows,
        projection_rows=log,
        projection_state=state,
        since=SINCE,
        until=UNTIL,
        later_projection_rows=_row_log(later_rows),
    )
    assert report.state_after_horizon == ("claim:alpha", KEY)
    assert report.exact is True


def test_wrong_timestamp_equal_to_horizon_is_not_excluded() -> None:
    log, state = _project(ROWS)
    state[KEY]["opened_at"] = HORIZON
    report = compare(
        projection_raw_rows={
            hashlib.sha256(raw.encode()).hexdigest(): raw for raw in ROWS
        },
        file_rows=ROWS,
        projection_rows=log,
        projection_state=state,
        since=SINCE,
        until=UNTIL,
    )
    assert report.state_after_horizon == ()
    assert [m.kind for m in report.mismatches] == [
        EnumParityMismatchKind.STATE_OPENED_AT_DIFFERS
    ]


def test_json_later_row_loader_filters_strictly_after_horizon(tmp_path: Path) -> None:
    log = _row_log([*ROWS, *LATER_ROWS])
    export = tmp_path / "projection.json"
    export.write_text(
        json.dumps(
            {"rows": [{"row_id": key, "row_ts": value} for key, value in log.items()]}
        )
    )
    assert load_projection_later_rows_json(export, HORIZON) == _row_log(LATER_ROWS)


def test_cli_reports_after_horizon_count_and_accepts_explicit_horizon(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log, state = _project(ROWS)
    log.update(_row_log(ROWS[2:]))
    ledger = tmp_path / "ledger.md"
    ledger.write_text("\n".join(ROWS) + "\n")
    export = tmp_path / "projection.json"
    export.write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "row_id": key,
                        "row_ts": value,
                        "raw_row": next(
                            raw
                            for raw in [*ROWS, *LATER_ROWS]
                            if hashlib.sha256(raw.encode()).hexdigest() == key
                        ),
                    }
                    for key, value in log.items()
                ],
                "state": [{"entity_key": key, **value} for key, value in state.items()],
            },
            default=str,
        )
    )
    args = [
        "--ledger", str(ledger), "--projection-json", str(export),
        "--since", "2026-10-20T00:00:00Z", "--until", "2026-10-20T23:59:59Z",
        "--format", "text",
    ]  # fmt: skip
    assert main(args) == 0
    assert "entities=1 state_after_horizon=0 mismatches=0" in capsys.readouterr().out
    assert main([*args, "--state-horizon", "2026-10-20T23:59:59Z"]) == 0
    assert "entities=1 state_after_horizon=1 mismatches=0" in capsys.readouterr().out
