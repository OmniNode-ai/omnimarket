# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24683: the context-source sequence reader on a real Postgres projection table.

A database double returns whatever columns the test hands it. Only a real table shows that the reader's SQL
names ``ledger_seq_provisional`` and that a projection-assigned number is not read as the judge's. The rows are
written by the real projection writer (``WorkLedgerProjectionWriter``), so the provisional number is the one the
production writer assigns, not one the test invents.

The case of the third review of omnimarket#6306: the judge's rows 1-3, a live unsequenced row that takes the
provisional number 4, and the judge's row 4 not yet arrived. ``read_sequence(through_seq=4)`` must not report the
sequence complete, so the revision ``ledger_seq:rolling-work-ledger:4`` reads STALE until the judge's own row 4
arrives; after it arrives the snapshot holds the judge's rows 1-4 and the moved provisional row stays unsequenced, so the
revision stays STALE rather than resolving with a row the judge never sequenced.

It SKIPS (never ERRORs) without a reachable database; the throwaway schema is the one the write-path gate builds.
"""

from __future__ import annotations

import pytest
from omnibase_core.enums.enum_context_source_status import EnumContextSourceStatus

from omnimarket.events.enum_ledger_row_type import EnumLedgerRowType
from omnimarket.nodes.node_context_source_ledger_effect.handlers.ledger_row_reader import (
    ModelLedgerSourceBinding,
    PostgresLedgerRowReader,
    reached_sequence,
)
from omnimarket.nodes.node_projection_work_ledger.handlers.handler_work_ledger_projection import (
    WorkLedgerProjectionWriter,
)
from tests.test_work_ledger_real_postgres_write_path import (
    _dsn,
    _meta,
    _migrated,
)
from tests.unit.nodes.node_context_source_ledger_effect.test_context_source_ledger import (
    LONG_WORDS,
    _handler,
    _request,
)

pytestmark = pytest.mark.integration

LEDGER = "rolling-work-ledger"
DSN_ENV = "OMN24683_CONTEXT_SOURCE_DSN"


def _ruling(n: int) -> str:
    return (
        f"2026-11-15T10:{n:02d}:00Z | RULING | lane=orch | ticket=OMN-{200 + n} | "
        f'kind=decision | "{LONG_WORDS.strip()} (ruling {n})" | answer {n}'
    )


async def _arrive(writer: WorkLedgerProjectionWriter, n: int, seq: int | None) -> None:
    topic = EnumLedgerRowType.RULING.topic
    await writer.project_event(
        topic,
        {"raw_row": _ruling(n), "ledger_seq": seq, "source": "onex-ledger"},
        _meta(topic),
    )


def _reader(schema: str, monkeypatch: pytest.MonkeyPatch) -> PostgresLedgerRowReader:
    monkeypatch.setenv(DSN_ENV, _dsn())
    return PostgresLedgerRowReader(
        ModelLedgerSourceBinding(
            relation=f"{schema}.work_ledger_rows",
            dsn_env=DSN_ENV,
            binding_ref="omn24683-real-postgres",
        )
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_provisional_row_is_not_the_judges_number_on_a_real_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Judge rows 1-3 plus a live row that took provisional 4: through_seq 4 is not complete; judge 4 completes it."""
    async with _migrated() as (writer, connection, schema):
        for n in (1, 2, 3):
            await _arrive(writer, n, n)
        await _arrive(writer, 7, None)  # a live unsequenced row: provisional number 4
        held = await connection.fetchrow(
            f"SELECT ledger_seq, ledger_seq_provisional FROM {schema}.work_ledger_rows WHERE raw_row = $1",
            _ruling(7),
        )
        assert held is not None
        assert (held["ledger_seq"], held["ledger_seq_provisional"]) == (4, True)

        reader = _reader(schema, monkeypatch)
        try:
            rows = reader.read_sequence(ledger_id=LEDGER, through_seq=4)
            sequences = tuple(row.ledger_seq for row in rows)
            assert sorted(s or 0 for s in sequences) == [0, 1, 2, 3]
            assert reached_sequence(sequences, 4) == (3, False)
            stale = _handler(reader).handle(_request(f"ledger_seq:{LEDGER}:4"))
            assert stale.status is EnumContextSourceStatus.STALE
            assert stale.watermark_reached == 3
            assert not stale.entries

            # The judge's row 4 arrives: the provisional row moves above the tip (now 5). It is still an
            # unsequenced row, so the snapshot holds the judge's 1-4 (watermark 4) and stays STALE, as it would
            # for a legacy NULL row; it never resolves complete with a row the judge did not sequence.
            await _arrive(writer, 4, 4)
            rows = reader.read_sequence(ledger_id=LEDGER, through_seq=4)
            sequences = tuple(row.ledger_seq for row in rows)
            assert sorted(s or 0 for s in sequences) == [0, 1, 2, 3, 4]
            assert reached_sequence(sequences, 4) == (4, False)
            moved = await connection.fetchrow(
                f"SELECT ledger_seq, ledger_seq_provisional FROM {schema}.work_ledger_rows WHERE raw_row = $1",
                _ruling(7),
            )
            assert moved is not None
            assert (moved["ledger_seq"], moved["ledger_seq_provisional"]) == (5, True)
        finally:
            reader.close()
