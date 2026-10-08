# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Gap report, read boundary, node contract and CLI coverage."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnimarket.handlers.work_ledger_seq_gap import (
    PostgresWorkLedgerSeqReader,
    build_report,
    report_from_seqs,
)
from omnimarket.models.model_work_ledger_seq_gap_report import (
    MAX_RANGES,
    WorkLedgerSeqFacts,
)
from omnimarket.nodes.node_work_ledger_seq_gap_effect import (
    HandlerWorkLedgerSeqGap,
)
from omnimarket.nodes.node_work_ledger_seq_gap_effect.gap_check import main
from omnimarket.nodes.node_work_ledger_seq_gap_effect.models import (
    ModelWorkLedgerSeqGapRequest,
)

pytestmark = pytest.mark.unit


def test_contiguous_is_exact() -> None:
    report = report_from_seqs([1, 2, 3], ledger_id="ledger")
    assert report.exact
    assert report.contiguous_through == report.to_seq == report.max_seq == 3
    assert report.missing_count == report.duplicate_count == report.tail_missing == 0


def test_internal_gaps() -> None:
    report = report_from_seqs([1, 2, 4, 5, 7], ledger_id="ledger")
    assert not report.exact
    assert report.first_missing_seq == 3
    assert report.missing_count == 2
    assert [(r.first, r.last) for r in report.missing_ranges] == [(3, 3), (6, 6)]
    assert report.contiguous_through == 2


def test_leading_gap() -> None:
    report = report_from_seqs([2, 3], ledger_id="ledger", from_seq=1)
    assert report.first_missing_seq == 1
    assert report.contiguous_through is None
    assert report.missing_count == 1


def test_duplicates_fail_exactness() -> None:
    report = report_from_seqs([1, 2, 2, 3], ledger_id="ledger")
    assert not report.exact
    assert report.duplicate_seqs == (2,)
    assert report.duplicate_count == 1
    assert report.rows_with_seq == 4


def test_expected_tail_loss() -> None:
    report = report_from_seqs([1, 2, 3], ledger_id="ledger", expected_max_seq=5)
    assert not report.exact
    assert report.tail_missing == 2
    assert report.missing_count == 0
    assert report.to_seq == 3


def test_empty_fails_exactness() -> None:
    report = report_from_seqs([], ledger_id="ledger", rows_without_seq=7)
    assert not report.exact
    assert (
        report.to_seq is report.contiguous_through is report.first_missing_seq is None
    )
    assert report.rows_without_seq == 7
    assert (
        report_from_seqs([], ledger_id="ledger", expected_max_seq=5).tail_missing == 5
    )


def test_ranges_truncated_but_total_complete() -> None:
    report = report_from_seqs(range(1, 123, 2), ledger_id="ledger")
    assert report.missing_count == 60
    assert len(report.missing_ranges) == MAX_RANGES
    assert report.missing_ranges_truncated
    assert report.missing_ranges[-1].first == 100


def test_duplicate_details_bounded_and_count_is_distinct() -> None:
    report = report_from_seqs(list(range(1, 62)) * 3, ledger_id="ledger")
    assert len(report.duplicate_seqs) == 50
    assert report.duplicate_count == 61
    assert report.duplicate_seqs == tuple(range(1, 51))


def test_from_seq_excludes_earlier_rows() -> None:
    report = report_from_seqs([1, 1, 2, 5, 6], ledger_id="ledger", from_seq=5)
    assert report.exact
    assert report.rows_with_seq == 2
    assert report.contiguous_through == 6


@pytest.mark.parametrize("seqs", [[0], [-1], [True], [1.5], ["1"]])
def test_offline_sequences_are_positive_integers(seqs: list[Any]) -> None:
    with pytest.raises(ValueError, match="positive integers"):
        report_from_seqs(seqs, ledger_id="ledger")


def test_build_report_counts_all_range_sizes() -> None:
    report = build_report(
        ledger_id="ledger",
        from_seq=1,
        gaps=[(5, 100)],
        duplicates=[],
        duplicate_count=0,
        rows_with_seq=3,
        rows_without_seq=0,
        min_seq=3,
        max_seq=101,
        max_seq_row_ts=None,
        newest_row_ts=None,
    )
    assert report.missing_count == 98
    assert report.first_missing_seq == 1
    assert [(r.first, r.last) for r in report.missing_ranges] == [(1, 2), (5, 100)]


class _FakeReader:
    def __init__(self) -> None:
        self.params: dict[str, Any] = {}

    def read_gap_facts(
        self,
        *,
        ledger_id: str,
        from_seq: int,
        since: datetime | None,
        until: datetime | None,
    ) -> WorkLedgerSeqFacts:
        self.params = {
            "ledger_id": ledger_id,
            "from_seq": from_seq,
            "since": since,
            "until": until,
        }
        return WorkLedgerSeqFacts(
            ledger_id=ledger_id,
            from_seq=from_seq,
            gaps=(),
            duplicates=(),
            duplicate_count=0,
            rows_with_seq=3,
            rows_without_seq=2,
            min_seq=from_seq,
            max_seq=from_seq + 2,
            max_seq_row_ts=until,
            newest_row_ts=until,
            window_since=since,
            window_until=until,
        )


def test_handler_uses_fake_reader_and_expected_tail() -> None:
    reader = _FakeReader()
    request = ModelWorkLedgerSeqGapRequest(correlation_id=uuid4(), expected_max_seq=5)
    report = HandlerWorkLedgerSeqGap(reader).handle(request)
    assert reader.params == {
        "ledger_id": "rolling-work-ledger",
        "from_seq": 1,
        "since": None,
        "until": None,
    }
    assert report.tail_missing == 2
    assert report.rows_without_seq == 2
    assert not report.exact


def test_handler_propagates_reader_errors() -> None:
    class BrokenReader(_FakeReader):
        def read_gap_facts(self, **kwargs: Any) -> WorkLedgerSeqFacts:
            raise RuntimeError("unreadable ledger")

    with pytest.raises(RuntimeError, match="unreadable ledger"):
        HandlerWorkLedgerSeqGap(BrokenReader()).handle(
            ModelWorkLedgerSeqGapRequest(correlation_id=uuid4())
        )


@pytest.mark.parametrize(
    "window",
    [
        {"since": "2026-10-08T00:00:00Z"},
        {"until": "2026-10-08T00:00:00Z"},
        {"since": "2026-10-08T00:00:00", "until": "2026-10-09T00:00:00Z"},
        {"since": "2026-10-08T00:00:00+01:00", "until": "2026-10-09T00:00:00Z"},
        {"since": "2026-10-09T00:00:00Z", "until": "2026-10-08T00:00:00Z"},
    ],
)
def test_request_rejects_invalid_window(window: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        ModelWorkLedgerSeqGapRequest.model_validate(
            {"correlation_id": uuid4(), **window}
        )


def test_request_and_report_are_frozen_and_forbid_extras() -> None:
    request = ModelWorkLedgerSeqGapRequest(correlation_id=uuid4())
    with pytest.raises(ValidationError):
        request.from_seq = 2
    with pytest.raises(ValidationError):
        ModelWorkLedgerSeqGapRequest.model_validate(
            {"correlation_id": uuid4(), "extra": "no"}
        )
    report = report_from_seqs([1], ledger_id="ledger")
    with pytest.raises(ValidationError):
        report.exact = False


@pytest.mark.parametrize(
    ("seqs", "code"), [([1, 2, 3], 0), ([1, 3], 1), ([1, 2, 2], 1), ([], 1)]
)
def test_cli_seqs_json_exit_codes(tmp_path: Path, seqs: list[int], code: int) -> None:
    path = tmp_path / "seqs.json"
    path.write_text(json.dumps(seqs))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnimarket.nodes.node_work_ledger_seq_gap_effect.gap_check",
            "--seqs-json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == code, result.stderr
    assert json.loads(result.stdout)["exact"] is (code == 0)


def test_cli_text_first_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "seqs.json"
    path.write_text("[1, 2, 4, 5, 7]")
    assert main(["--seqs-json", str(path), "--format", "text"]) == 1
    assert capsys.readouterr().out.splitlines()[0] == (
        "ledger_seq gap check: exact=no first_missing_seq=3 missing=2 duplicates=0 "
        "range=1..7 rows_with_seq=5 rows_without_seq=0"
    )


def test_cli_unset_dsn_redacted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("WORK_LEDGER_GAP_TEST_UNSET", raising=False)
    assert main(["--dsn-env", "WORK_LEDGER_GAP_TEST_UNSET"]) == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert "unset" in captured.err
    assert "postgresql://" not in captured.err


def test_cli_utc_day_and_expected_max(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "seqs.json"
    path.write_text("[1, 2, 3]")
    assert (
        main(
            [
                "--seqs-json",
                str(path),
                "--utc-day",
                "2026-10-08",
                "--expect-max-seq",
                "5",
            ]
        )
        == 1
    )
    report = json.loads(capsys.readouterr().out)
    assert report["window_since"] == "2026-10-08T00:00:00Z"
    assert report["window_until"] == "2026-10-08T23:59:59.999999Z"
    assert report["tail_missing"] == 2


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--since", "bad"],
        ["--from-seq", "0"],
        ["--utc-day", "2026-10-08", "--since", "2026-10-08T00:00:00Z"],
    ],
)
def test_cli_errors(
    tmp_path: Path, args: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "seqs.json"
    path.write_text("[0]")
    assert main(["--seqs-json", str(path), *args]) == 2
    assert not capsys.readouterr().out


class _Cursor:
    def __init__(self, row: tuple[Any, ...]) -> None:
        self.row = row
        self.sql = ""
        self.params: dict[str, Any] = {}

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def execute(self, sql: str, params: dict[str, Any]) -> None:
        self.sql, self.params = sql, params

    def fetchone(self) -> tuple[Any, ...]:
        return self.row


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor
        self.closed = False

    def cursor(self) -> _Cursor:
        return self._cursor

    def close(self) -> None:
        self.closed = True


def test_postgres_facts_use_one_readonly_query_and_effective_window_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnimarket.projection import postgres_read_database

    since = datetime(2026, 10, 8, tzinfo=UTC)
    until = datetime(2026, 10, 9, tzinfo=UTC)
    cursor = _Cursor((10, [[12, 13]], [11], 1, 5, 2, 10, 15, until, until))
    conn = _Connection(cursor)
    monkeypatch.setenv("WORK_LEDGER_GAP_TEST_DSN", "test-dsn")
    monkeypatch.setattr(postgres_read_database, "connect_read_only", lambda _dsn: conn)
    facts = PostgresWorkLedgerSeqReader("WORK_LEDGER_GAP_TEST_DSN").read_gap_facts(
        ledger_id="ledger'quote", from_seq=1, since=since, until=until
    )
    assert conn.closed
    assert cursor.params == {
        "ledger_id": "ledger'quote",
        "from_seq": 1,
        "since": since,
        "until": until,
    }
    assert "ledger'quote" not in cursor.sql
    assert "lead(ledger_seq) OVER" in cursor.sql
    assert "GROUP BY ledger_seq" in cursor.sql
    assert "LIMIT 50" in cursor.sql
    assert "FROM omninode_internal.work_ledger_rows, bounds" in cursor.sql
    assert facts.from_seq == 10
    assert facts.gaps == ((12, 13),)
    assert facts.duplicates == (11,)
    assert facts.newest_row_ts == until


@pytest.mark.parametrize(
    "relation", ["work_ledger_rows", "a.b; DROP TABLE c", "A.b", "a.b.c"]
)
def test_postgres_rejects_unsafe_relation(relation: str) -> None:
    with pytest.raises(ValueError, match="unsafe"):
        PostgresWorkLedgerSeqReader("ENV", relation)


@pytest.mark.parametrize(
    "message",
    [
        "cannot connect postgresql://user:secret@host/db",
        "cannot connect postgres://user:secret@host/db",
        "password=secret host=host",
        "test-dsn failed",
    ],
)
def test_postgres_errors_are_redacted(
    monkeypatch: pytest.MonkeyPatch, message: str
) -> None:
    from omnimarket.projection import postgres_read_database

    def fail(dsn: str) -> None:
        raise RuntimeError(message)

    monkeypatch.setenv("WORK_LEDGER_GAP_TEST_DSN", "test-dsn")
    monkeypatch.setattr(postgres_read_database, "connect_read_only", fail)
    with pytest.raises(RuntimeError) as error:
        PostgresWorkLedgerSeqReader("WORK_LEDGER_GAP_TEST_DSN").read_gap_facts(
            ledger_id="ledger", from_seq=1, since=None, until=None
        )
    assert "secret" not in str(error.value)
    assert "test-dsn" not in str(error.value)
    assert "postgresql://" not in str(error.value)
    assert error.value.__cause__ is None
