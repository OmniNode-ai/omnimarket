# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Parity compares row text, parsed authority fields and relationship closure (OMN-21107, TA.5).

The check compares, per row, the projected ``raw_row`` with the markdown row
text and the authority fields parsed from each, and per entity the
relationship closure re-derived from each side's rows. Negative controls: a
projection copy with one RULING row's words changed (id and timestamps kept)
reports a text mismatch, and one with a ``corrects=`` link dropped reports a
text, an authority and a relationship mismatch.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers.work_ledger_fold import apply_ops, fold_row
from omnimarket.models.model_work_ledger_fold_request import (
    ModelWorkLedgerFoldRequest,
)
from omnimarket.nodes.node_projection_work_ledger.models.model_work_ledger_parity_report import (
    EnumParityMismatchKind,
)
from omnimarket.nodes.node_projection_work_ledger.parity import (
    compare,
    main,
    receipt_row,
)

pytestmark = pytest.mark.unit

RULING = '2026-10-01T09:00:00Z | RULING | lane=orch | ticket=OMN-1 | kind=decision | "use the lab" | first answer'
CORRECTION = "2026-10-01T09:10:00Z | CORRECTION | lane=orch | ticket=OMN-1 | corrects=2026-10-01T09:00:00Z | operator_span=0:11 | the quoted cell holds framing"
CONSENT = '2026-10-01T09:05:00Z | OPERATOR-CONSENT | lane=orch | "yes" | APPROVED SCOPE: lab | OUT OF SCOPE: prod | evidence'
SUPERSEDING = '2026-10-01T10:00:00Z | RULING | lane=orch | ticket=OMN-1 | kind=decision | supersedes=2026-10-01T09:05:00Z | "use the pool" | replaces the consent'
CLAIM = "2026-10-01T09:20:00Z | CLAIM | lane=alpha | ticket=OMN-1 | est ~1 lane-hours; displaces x; (OMN-1) | work"
TERMINAL = (
    "2026-10-01T09:30:00Z | TERMINAL | lane=alpha | ticket=OMN-1 | friction=none | done"
)
ROWS = [RULING, CONSENT, CORRECTION, CLAIM, TERMINAL, SUPERSEDING]

RULING_KEY = "ruling:2026-10-01T09:00:00Z:orch"
CONSENT_KEY = "consent:2026-10-01T09:05:00Z:orch"
SINCE = datetime(2026, 10, 1, tzinfo=UTC)
UNTIL = datetime(2026, 10, 1, 23, 59, 59, tzinfo=UTC)


def _rid(raw: str) -> str:
    return fold_row(ModelWorkLedgerFoldRequest(raw_row=raw)).row.row_id


def _project(
    rows: list[str],
) -> tuple[dict[str, str], dict[str, dict[str, Any]], dict[str, str]]:
    """A faithful projection: row stamps, folded state and the stored raw rows."""
    log: dict[str, str] = {}
    raw_rows: dict[str, str] = {}
    state: dict[str, dict[str, object]] = {}
    for raw in rows:
        result = fold_row(ModelWorkLedgerFoldRequest(raw_row=raw))
        log[result.row.row_id] = f"{result.row.row_ts:%Y-%m-%dT%H:%M:%SZ}"
        raw_rows[result.row.row_id] = result.row.raw_row
        apply_ops(state, result.ops)
    return log, {k: dict(v) for k, v in state.items()}, raw_rows


def _compare(raw_rows: dict[str, str] | None, **overrides: Any) -> Any:
    log, state, faithful = _project(ROWS)
    return compare(
        file_rows=ROWS,
        projection_rows=log,
        projection_state=state,
        since=SINCE,
        until=UNTIL,
        projection_raw_rows=faithful if raw_rows is None else raw_rows,
        **overrides,
    )


def _kinds(report: Any) -> list[tuple[EnumParityMismatchKind, str]]:
    return [(m.kind, m.key) for m in report.mismatches]


def test_a_faithful_projection_is_exact_over_text_authority_and_relations() -> None:
    report = _compare(None)
    assert report.mismatches == ()
    assert report.exact is True
    assert report.rows_text_compared == len(ROWS)
    # Every entity touched by the window has its closure compared.
    assert report.relations_compared == report.state_entities_compared > 0


def test_negative_control_changed_ruling_words_is_a_text_mismatch() -> None:
    _log, _state, raw_rows = _project(ROWS)
    rid = _rid(RULING)
    raw_rows[rid] = RULING.replace('"use the lab"', '"use the cloud"')
    report = _compare(raw_rows)
    assert report.exact is False
    # The id and timestamps are kept, so no row is missing and no state differs:
    # the words are not an authority field, so only the text check sees it.
    assert _kinds(report) == [(EnumParityMismatchKind.ROW_TEXT_DIFFERS, rid)]


def test_negative_control_dropped_corrects_link_is_a_relation_mismatch() -> None:
    _log, _state, raw_rows = _project(ROWS)
    rid = _rid(CORRECTION)
    raw_rows[rid] = CORRECTION.replace(" corrects=2026-10-01T09:00:00Z |", "")
    report = _compare(raw_rows)
    kinds = _kinds(report)
    assert (EnumParityMismatchKind.ROW_TEXT_DIFFERS, rid) in kinds
    assert (EnumParityMismatchKind.ROW_AUTHORITY_DIFFERS, rid) in kinds
    assert (EnumParityMismatchKind.STATE_RELATIONS_DIFFER, RULING_KEY) in kinds
    authority = next(
        m for m in report.mismatches
        if m.kind is EnumParityMismatchKind.ROW_AUTHORITY_DIFFERS
    )  # fmt: skip
    assert "corrects" in authority.detail
    relation = next(
        m for m in report.mismatches
        if m.kind is EnumParityMismatchKind.STATE_RELATIONS_DIFFER
    )  # fmt: skip
    assert "corrected_by" in relation.detail
    assert rid in relation.detail


def test_a_dropped_supersedes_link_is_named_on_its_target() -> None:
    _log, _state, raw_rows = _project(ROWS)
    rid = _rid(SUPERSEDING)
    raw_rows[rid] = SUPERSEDING.replace(" supersedes=2026-10-01T09:05:00Z |", "")
    report = _compare(raw_rows)
    relations = [
        m for m in report.mismatches
        if m.kind is EnumParityMismatchKind.STATE_RELATIONS_DIFFER
    ]  # fmt: skip
    assert [m.key for m in relations] == [CONSENT_KEY]
    assert "superseded_by" in relations[0].detail


def test_a_projected_row_without_its_text_is_named() -> None:
    _log, _state, raw_rows = _project(ROWS)
    rid = _rid(CLAIM)
    del raw_rows[rid]
    report = _compare(raw_rows)
    assert (EnumParityMismatchKind.ROW_TEXT_MISSING_IN_PROJECTION, rid) in _kinds(
        report
    )


def test_a_projected_row_that_no_longer_parses_is_an_authority_mismatch() -> None:
    _log, _state, raw_rows = _project(ROWS)
    rid = _rid(TERMINAL)
    raw_rows[rid] = "not a ledger row"
    report = _compare(raw_rows)
    kinds = _kinds(report)
    assert (EnumParityMismatchKind.ROW_TEXT_DIFFERS, rid) in kinds
    assert (EnumParityMismatchKind.ROW_AUTHORITY_DIFFERS, rid) in kinds


def test_without_projected_text_every_shared_row_is_named() -> None:
    log, state, _raw = _project(ROWS)
    report = compare(
        file_rows=ROWS,
        projection_rows=log,
        projection_state=state,
        since=SINCE,
        until=UNTIL,
    )
    assert report.exact is False
    assert report.rows_text_compared == 0
    assert {
        m.key
        for m in report.mismatches
        if m.kind is EnumParityMismatchKind.ROW_TEXT_MISSING_IN_PROJECTION
    } == set(log)


def test_receipt_counts_text_and_relation_mismatches() -> None:
    _log, _state, raw_rows = _project(ROWS)
    raw_rows[_rid(CORRECTION)] = CORRECTION.replace(
        " corrects=2026-10-01T09:00:00Z |", ""
    )
    report = _compare(raw_rows)
    cells = dict(
        c.split("=", 1)
        for c in receipt_row(
            report, "2026-10-01", datetime(2026, 10, 2, tzinfo=UTC)
        ).split(" | ")
        if "=" in c
    )
    assert (cells["text_mismatches"], cells["state_mismatches"], cells["exact"]) == (
        "2",
        "1",
        "no",
    )


def _export(path: Path, raw_rows: dict[str, str]) -> None:
    log, state, _faithful = _project(ROWS)
    path.write_text(
        json.dumps(
            {
                "rows": [
                    {"row_id": k, "row_ts": v, "raw_row": raw_rows[k]}
                    for k, v in log.items()
                ],
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


def test_cli_reads_the_exported_text_and_reports_the_negative_control(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = tmp_path / "ROLLING_WORK_LEDGER.md"
    ledger.write_text("# ledger\n" + "\n".join(ROWS) + "\n")
    export = tmp_path / "projection.json"
    _log, _state, raw_rows = _project(ROWS)
    _export(export, raw_rows)
    args = [
        "--ledger", str(ledger), "--since", "2026-10-01T00:00:00Z",
        "--until", "2026-10-01T23:59:59Z", "--projection-json", str(export),
    ]  # fmt: skip
    assert main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mismatches"] == []
    assert report["rows_text_compared"] == len(ROWS)

    raw_rows[_rid(RULING)] = RULING.replace('"use the lab"', '"use the cloud"')
    _export(export, raw_rows)
    assert main(args) == 1
    report = json.loads(capsys.readouterr().out)
    assert [m["kind"] for m in report["mismatches"]] == ["row_text_differs"]
