# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The work-ledger context source's explicit sequence opt-in (OMN-21127 TC.4).

A revision of the form ``ledger_seq:<ledger_id>:<positive integer>`` asks the
source for a ledger's authority as of a sequence number instead of a row
stamp. The handler must call ``reader.read_sequence(ledger_id=...,
through_seq=...)`` -- every row of that ledger with ``ledger_seq <=
through_seq`` or a NULL ``ledger_seq``, timestamps ignored -- and fold the
rows only when the returned sequence is complete: the highest contiguous run
from 1, stopping before any duplicated seq, no NULL legacy row, reaching
``through_seq``. Anything else is ``STALE`` with no entries and
``watermark_reached`` at the reached prefix; an incomplete authority is never
folded. A complete sequence reduces through the existing entries reduction at
the max raw-row timestamp. A reader without the sequence capability is
``UNAVAILABLE``, never a silent timestamp fallback.

Every sequence row below shares one timestamp on purpose: if timestamps could
establish completeness, these fixtures could not tell a sequence read from a
timestamp read.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from omnibase_core.enums.enum_context_source_error_code import (
    EnumContextSourceErrorCode,
)
from omnibase_core.enums.enum_context_source_status import EnumContextSourceStatus

from omnimarket.nodes.node_context_source_ledger_effect.handlers.ledger_row_reader import (
    ModelLedgerSourceRow,
)
from omnimarket.nodes.node_context_source_ledger_effect.models import (
    ModelLedgerSourceResult,
)
from omnimarket.nodes.node_work_ledger_emit_effect.handlers.row_parser import (
    row_id_of,
)
from tests.unit.nodes.node_context_source_ledger_effect.test_context_source_ledger import (
    DAY,
    LONG_WORDS,
    TENANT,
    _handler,
    _request,
)

pytestmark = pytest.mark.unit

LEDGER = "lab-main"
# One stamp for every sequence row: completeness must come from ledger_seq.
SEQ_STAMP = "2026-10-01T00:00:00Z"


def _seq_revision(through_seq: int) -> str:
    return f"ledger_seq:{LEDGER}:{through_seq}"


def _seq_rows(*seqs: int | None) -> tuple[ModelLedgerSourceRow, ...]:
    """One independent open RULING per position, all at the shared stamp.

    Tickets and quoted cells differ per position, so a repeated seq arrives as
    two genuinely distinct rows, the way a duplicated write would.
    """
    rows = []
    for index, seq in enumerate(seqs, start=1):
        raw = (
            f"{SEQ_STAMP} | RULING | lane=orch | ticket=OMN-{200 + index} | "
            f'kind=decision | "{LONG_WORDS.strip()} (ruling {index})" '
            f"| answer {index}"
        )
        rows.append(
            ModelLedgerSourceRow(
                row_id=row_id_of(raw),
                raw_row=raw,
                source_ref=f"work_ledger_rows:{row_id_of(raw)}",
                ledger_seq=seq,
            )
        )
    return tuple(rows)


def _day_rows() -> tuple[ModelLedgerSourceRow, ...]:
    """The timestamp path's rows: the DAY fixture, no ledger_seq."""
    return tuple(
        ModelLedgerSourceRow(
            row_id=row_id_of(raw),
            raw_row=raw,
            source_ref=f"work_ledger_rows:{row_id_of(raw)}",
        )
        for raw in DAY
    )


class _RecordingReader:
    """Serves canned rows for either read shape and records every call."""

    def __init__(
        self,
        *,
        rows: tuple[ModelLedgerSourceRow, ...] = (),
        sequence: tuple[ModelLedgerSourceRow, ...] = (),
    ) -> None:
        self._rows = rows
        self._sequence = sequence
        self.calls: list[tuple[str, dict[str, object]]] = []

    def read_rows(self, *, as_of: datetime) -> tuple[ModelLedgerSourceRow, ...]:
        self.calls.append(("read_rows", {"as_of": as_of}))
        return self._rows

    def read_sequence(
        self, *, ledger_id: str, through_seq: int
    ) -> tuple[ModelLedgerSourceRow, ...]:
        self.calls.append(
            ("read_sequence", {"ledger_id": ledger_id, "through_seq": through_seq})
        )
        return self._sequence


class _TimestampOnlyReader:
    """A reader from before TC.4: no read_sequence capability at all."""

    def __init__(self) -> None:
        self.row_reads = 0

    def read_rows(self, *, as_of: datetime) -> tuple[ModelLedgerSourceRow, ...]:
        self.row_reads += 1
        return ()


@pytest.mark.parametrize(
    "seqs",
    [
        pytest.param((1,), id="single"),
        pytest.param((1, 2, 3), id="three"),
    ],
)
def test_a_complete_sequence_resolves_whole_rows_through_read_sequence(
    seqs: tuple[int, ...],
) -> None:
    through = len(seqs)
    rows = _seq_rows(*seqs)
    reader = _RecordingReader(sequence=rows)
    result = _handler(reader).handle(_request(_seq_revision(through)))

    assert result.status is EnumContextSourceStatus.RESOLVED
    assert result.watermark_reached == through
    # Exactly one read, of the sequence shape, with the parsed revision parts.
    assert reader.calls == [
        ("read_sequence", {"ledger_id": LEDGER, "through_seq": through})
    ]
    assert len(result.entries) == len(rows)
    by_row_id = {row.row_id: row for row in rows}
    for entry in result.entries:
        row = by_row_id[entry.row_id]
        # The whole raw row survives, never a display cut, whatever its length.
        assert entry.text == row.raw_row
        assert len(entry.text) > 900
        assert entry.text_sha256 == hashlib.sha256(entry.text.encode()).hexdigest()
        assert entry.verbatim_verified
        assert entry.span is not None
        assert entry.span.start == 0
        assert entry.span.end == len(entry.text)
        assert entry.source_ref == row.source_ref
        # The existing fold keys authority by stamp: multiple rows sharing it
        # stay explicitly ambiguous even though the sequence snapshot is complete.
        assert entry.status is (
            EnumContextSourceStatus.RESOLVED
            if len(rows) == 1
            else EnumContextSourceStatus.AMBIGUOUS
        )
        # Independent rulings: nothing supersedes or corrects them.
        from omnibase_core.enums.enum_context_supersession_status import (
            EnumContextSupersessionStatus,
        )

        assert entry.supersession_status is EnumContextSupersessionStatus.ACTIVE
        assert entry.corrected_by == ()
    # The reduction ran at the max raw-row timestamp: here the shared stamp.
    assert all(
        entry.as_of == datetime(2026, 10, 1, 0, 0, 0, tzinfo=UTC)
        for entry in result.entries
    )


def test_a_permuted_complete_sequence_returns_the_same_entries() -> None:
    def resolved(rows: tuple[ModelLedgerSourceRow, ...]) -> ModelLedgerSourceResult:
        reader = _RecordingReader(sequence=rows)
        result = _handler(reader).handle(_request(_seq_revision(3)))
        assert result.status is EnumContextSourceStatus.RESOLVED
        assert result.watermark_reached == 3
        return result

    base = resolved(_seq_rows(1, 2, 3))
    permuted = resolved(_seq_rows(3, 1, 2))
    # Arrival order decides neither the contiguity verdict nor the entries.
    assert [e.row_id for e in permuted.entries] == [e.row_id for e in base.entries]
    assert [e.text for e in permuted.entries] == [e.text for e in base.entries]


@pytest.mark.parametrize(
    ("why", "seqs", "through", "watermark"),
    [
        pytest.param("gap after 1", (1, 3), 3, 1, id="gap"),
        pytest.param("duplicate 2", (1, 2, 2, 3), 3, 1, id="duplicate"),
        pytest.param("behind the ask", (1, 2), 3, 2, id="behind"),
        pytest.param("seq 1 missing", (2, 3), 3, 0, id="missing-first"),
        pytest.param("nothing read", (), 3, 0, id="empty"),
        pytest.param("null legacy row", (1, None, 2), 2, 2, id="null-legacy"),
    ],
)
def test_an_incomplete_sequence_is_stale_with_no_entries_and_the_reached_prefix(
    why: str,
    seqs: tuple[int | None, ...],
    through: int,
    watermark: int,
) -> None:
    reader = _RecordingReader(sequence=_seq_rows(*seqs))
    result = _handler(reader).handle(_request(_seq_revision(through)))

    assert result.status is EnumContextSourceStatus.STALE, why
    assert not result.entries
    # The watermark is the highest contiguous run from 1, stopping before any
    # duplicated seq; a NULL legacy row flags stale without truncating it, and
    # a behind read stops at the last seq actually present.
    assert result.watermark_reached == watermark
    assert reader.calls == [
        ("read_sequence", {"ledger_id": LEDGER, "through_seq": through})
    ]


def test_the_timestamp_revision_still_reads_rows_and_leaves_the_watermark_none() -> (
    None
):
    reader = _RecordingReader(rows=_day_rows())
    result = _handler(reader).handle(_request("2026-10-01T23:59:59Z"))

    assert result.status is EnumContextSourceStatus.RESOLVED
    assert result.entries
    assert result.watermark_reached is None
    # The old path reads rows at the parsed stamp and never the sequence read.
    assert reader.calls == [
        ("read_rows", {"as_of": datetime(2026, 10, 1, 23, 59, 59, tzinfo=UTC)})
    ]


def test_denial_precedes_any_read() -> None:
    reader = _RecordingReader(sequence=_seq_rows(1, 2, 3))
    result = _handler(reader).handle(_request(_seq_revision(3), tenant=f"not-{TENANT}"))

    assert result.status is EnumContextSourceStatus.DENIED
    assert not result.entries
    assert result.watermark_reached is None
    assert reader.calls == []


def test_a_reader_without_read_sequence_is_unavailable_without_a_fallback() -> None:
    reader = _TimestampOnlyReader()
    result = _handler(reader).handle(_request(_seq_revision(3)))

    assert result.status is EnumContextSourceStatus.UNAVAILABLE
    assert not result.entries
    assert result.watermark_reached is None
    # A capability gap must not degrade into a timestamp read.
    assert reader.row_reads == 0


@pytest.mark.parametrize(
    "revision",
    [
        pytest.param("ledger_seq", id="prefix-only"),
        pytest.param("ledger_seq:", id="empty-rest"),
        pytest.param("ledger_seq:lab-main", id="no-through-seq"),
        pytest.param("ledger_seq:lab-main:", id="empty-through-seq"),
        pytest.param("ledger_seq::3", id="empty-ledger-id"),
        pytest.param("ledger_seq:lab-main:0", id="zero"),
        pytest.param("ledger_seq:lab-main:9223372036854775808", id="bigint-overflow"),
        pytest.param("ledger_seq:lab-main:" + "9" * 5000, id="unbounded-integer"),
        pytest.param("ledger_seq:lab main:3", id="whitespace-ledger"),
        pytest.param("ledger_seq:lab-main:-2", id="negative"),
        pytest.param("ledger_seq:lab-main:1.5", id="float"),
        pytest.param("ledger_seq:lab-main:03x", id="trailing-junk"),
        pytest.param("ledger_seq:lab-main:3:extra", id="extra-segment"),
    ],
)
def test_malformed_sequence_revisions_are_typed_invalid_requests_before_any_read(
    revision: str,
) -> None:
    reader = _RecordingReader(sequence=_seq_rows(1, 2, 3))
    result = _handler(reader).handle(_request(revision))

    assert result.status is EnumContextSourceStatus.UNAVAILABLE
    assert result.error_code is EnumContextSourceErrorCode.INVALID_REQUEST
    assert not result.entries
    assert result.watermark_reached is None
    assert reader.calls == []


class _LegacyReader:
    def read_rows(self, *, as_of: datetime) -> tuple[ModelLedgerSourceRow, ...]:
        raise AssertionError("sequence request must not use timestamp completeness")

    def read_sequence(
        self, *, ledger_id: str, through_seq: int
    ) -> tuple[ModelLedgerSourceRow, ...]:
        return (
            ModelLedgerSourceRow(
                row_id="legacy", raw_row=DAY[0], source_ref="legacy:1"
            ),
        )


def test_legacy_unsequenced_history_is_stale_instead_of_resolved() -> None:
    result = _handler(_LegacyReader()).handle(_request("ledger_seq:work:3"))
    assert result.status is EnumContextSourceStatus.STALE
    assert not result.entries
    assert result.watermark_reached == 0


def test_rows_after_requested_sequence_are_not_exposed() -> None:
    rows = _seq_rows(1, 2, 3)
    result = _handler(_RecordingReader(sequence=rows)).handle(
        _request(_seq_revision(1))
    )
    assert result.status is EnumContextSourceStatus.RESOLVED
    assert result.watermark_reached == 1
    assert [entry.text for entry in result.entries] == [rows[0].raw_row]


def test_sequence_completeness_does_not_lose_correction_closure() -> None:
    from tests.unit.nodes.node_context_source_ledger_effect.test_context_source_ledger import (
        D1_CORRECTION,
        D1_RULING,
    )

    rows = tuple(
        ModelLedgerSourceRow(
            row_id=row_id_of(raw),
            raw_row=raw,
            source_ref=f"ledger:{seq}",
            ledger_seq=seq,
        )
        for seq, raw in enumerate((D1_RULING, D1_CORRECTION), 1)
    )
    result = _handler(_RecordingReader(sequence=rows[::-1])).handle(
        _request(_seq_revision(2))
    )
    assert result.status is EnumContextSourceStatus.RESOLVED
    assert result.watermark_reached == 2
    assert len(result.entries) == 1
    assert result.entries[0].text == D1_RULING
    assert result.entries[0].corrected_by[0].text == D1_CORRECTION


@pytest.mark.parametrize("fail", [False, True])
def test_postgres_sequence_read_is_scoped_parameterized_and_keeps_null_history(
    monkeypatch: pytest.MonkeyPatch,
    fail: bool,
) -> None:
    from omnimarket.nodes.node_context_source_ledger_effect.handlers.ledger_row_reader import (
        LedgerSourceReadError,
        ModelLedgerSourceBinding,
        PostgresLedgerRowReader,
    )

    executed: list[tuple[str, tuple[object, ...]]] = []
    rows = _seq_rows(None, 1, 2)

    class Cursor:
        def __enter__(self) -> Cursor:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def execute(self, query: object, params: tuple[object, ...]) -> None:
            executed.append((str(query), params))
            if fail:
                raise RuntimeError("postgresql://test-private-dsn")

        def fetchall(self) -> list[tuple[str, str, int | None]]:
            return [(row.row_id, row.raw_row, row.ledger_seq) for row in rows]

    class Connection:
        closed = False

        def cursor(self) -> Cursor:
            return Cursor()

        def close(self) -> None:
            self.closed = True

    reader = PostgresLedgerRowReader(
        ModelLedgerSourceBinding(
            relation="fixture.work_ledger_rows",
            dsn_env="TEST_LEDGER_SEQUENCE_DSN",
            binding_ref="test",
        )
    )
    connection = Connection()
    reader._conn = connection
    if fail:
        with pytest.raises(
            LedgerSourceReadError, match="connection/query failed"
        ) as error:
            reader.read_sequence(ledger_id="other-ledger", through_seq=2)
        assert "test-private-dsn" not in str(error.value)
        assert connection.closed
    else:
        result = reader.read_sequence(ledger_id="other-ledger", through_seq=2)
        assert [row.raw_row for row in result] == [row.raw_row for row in rows]
        assert [row.ledger_seq for row in result] == [None, 1, 2]
        assert not connection.closed
    assert len(executed) == 1
    query, params = executed[0]
    assert params == ("other-ledger", 2)
    assert "WHERE ledger_id = %s AND (ledger_seq <= %s OR ledger_seq IS NULL)" in query
    assert "row_ts" not in query
    assert "other-ledger" not in query
