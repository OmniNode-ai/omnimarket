# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The projection reads the ledger in one pass for all its row types (OMN-22935).

Measured on a lab host 2026-11-10: three separate reads of a 220 MB ledger took 3.5, 4.0 and 6.2 s, and the source's 10 s
attempt (the overlay's 20 s timeout over 2 attempts) timed out, so the ledger was unread and no item was offered as work.
One pass over the files answers every row type (OMN-23081). The failing control is a source with no batch port, which is
read once per row type and so passes over the files once per row type.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    FileLedgerRowSource,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelLedgerQuery,
    ModelLedgerRead,
)
from omnimarket.nodes.node_work_state_projection_effect.runtime_work_state import (
    source_readers,
)


class CountingSource(FileLedgerRowSource):
    def __init__(self, live: Path) -> None:
        super().__init__(live, None)
        self.batches: list[int] = []

    def read_many(
        self, queries: Sequence[ModelLedgerQuery]
    ) -> tuple[ModelLedgerRead, ...]:
        self.batches.append(len(queries))
        return super().read_many(queries)


class PerQuerySource:
    """No batch port: only ``read``."""

    def __init__(self, inner: FileLedgerRowSource) -> None:
        self._inner = inner
        self.reads = 0

    def read(self, query: ModelLedgerQuery) -> ModelLedgerRead:
        self.reads += 1
        return self._inner.read(query)


def _ledger(tmp_path: Path, now) -> Path:
    stamp = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    live = tmp_path / "rows.md"
    live.write_text(
        f"{stamp} | CLAIM | lane=a | ticket=ZEP-1 | text\n"
        f"{stamp} | STATUS | lane=a | note=x | text\n"
        f"{stamp} | TERMINAL | lane=a | outcome=done | text\n",
        encoding="utf-8",
    )
    return live


def _read(spec_source, tmp_path, now, rules):
    spec = next(
        s
        for s in source_readers(now, tmp_path, {}, spec_source, rules)
        if s.source.value == "ledger"
    )
    return spec.read()


def test_the_ledger_source_asks_for_all_row_types_in_one_pass(
    tmp_path, now, rules
) -> None:
    source = CountingSource(_ledger(tmp_path, now))
    read = _read(source, tmp_path, now, rules)
    assert source.batches == [3]
    assert sorted(r.row_type for r in read.items) == ["CLAIM", "STATUS", "TERMINAL"]


def test_failing_control_a_source_without_the_batch_port_is_read_once_per_row_type(
    tmp_path, now, rules
) -> None:
    source = PerQuerySource(FileLedgerRowSource(_ledger(tmp_path, now), None))
    read = _read(source, tmp_path, now, rules)
    assert source.reads == 3
    assert len(read.items) == 3
