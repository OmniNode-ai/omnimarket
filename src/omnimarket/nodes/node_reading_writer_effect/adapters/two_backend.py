# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reading writer's ledger row source on both backends, and the scenario that compares them.

``backends`` in contract.yaml names these (OMN-22911): the in-memory ledger is the default and the
file ledger is chosen by the overlay's ``ledger_read.route``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    FileLedgerRowSource,
    InMemoryLedger,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelLedgerQuery,
    ProtocolLedgerRowSource,
)

SEED_ROWS = (
    "2026-11-05T14:30:00Z | STATUS | lane=work-ledger-parity | principal=dev-test"
    " | missing=0 | unexplained=0 | req=p1 | via=bus:h200 | parity",
    "2026-11-05T14:40:00Z | STATUS | lane=landing-L1 | principal=dev-test"
    " | src=delegation | outcome=completed | req=d1 | via=bus:h200 | terminal",
)
WINDOW_START = datetime(2026, 11, 5, 14, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 11, 5, 15, 0, tzinfo=UTC)


def make_in_memory_backend(
    rows: Sequence[str], workdir: Path
) -> ProtocolLedgerRowSource:
    return InMemoryLedger(rows)


def make_file_backend(rows: Sequence[str], workdir: Path) -> ProtocolLedgerRowSource:
    live = workdir / "ledger.md"
    live.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return FileLedgerRowSource(live, None)


def scenario_row_roundtrip(
    factory: Callable[[Sequence[str], Path], ProtocolLedgerRowSource], workdir: Path
) -> object:
    """Seed two rows, read one lane back; both backends must return the same row text."""
    read = factory(SEED_ROWS, workdir).read(
        ModelLedgerQuery(
            since=WINDOW_START,
            until=WINDOW_END,
            row_type="STATUS",
            lane="work-ledger-parity",
        )
    )
    if read.error is not None:
        raise RuntimeError(read.error)
    return [row.raw for row in read.rows]
