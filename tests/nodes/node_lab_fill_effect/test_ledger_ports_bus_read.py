# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab-fill tick's ledger ports read the bus work ledger reader, never the ledger file (OMN-26330).

Each test runs with the ledger path variable unset and no ledger file anywhere, and hands the port a
stub rows reader: the old adapters opened the file named by their ``ledger_path`` argument, so these
fail on them and pass only when the rows come from the reader.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

import pytest

from omnimarket.handlers.adapter_work_ledger_rows_reader import (
    SubprocessLedgerRowsReader,
)
from omnimarket.nodes.node_lab_fill_effect.protocols.local_lab_fill_adapters import (
    LAB_FILL_PORTS_READER,
    LAB_FILL_PORTS_WINDOW_HOURS,
    LocalLedgerReader,
    LocalMergedWork,
    LocalOwnerReader,
)
from omnimarket.nodes.node_lab_fill_effect.protocols.protocol_lab_fill_effect import (
    LabFillPortError,
)
from omnimarket.protocols.protocol_ledger_rows_reader import LedgerReadError

_ROWS = (
    "2026-10-09T00:40:00Z | STATUS | lane=lab-fill | run=2026-10-09T0020Z | x=1",
    "2026-10-09T01:00:05Z | STATUS | lane=lab-fill | run=2026-10-09T0100Z | x=2",
    "2026-10-09T01:00:06Z | STATUS | lane=other | run=2026-10-09T0200Z | x=3",
    "2026-10-09T01:00:07Z | CLAIM | lane=lab-fill | run=2026-10-09T0300Z | x=4",
)


class _StubRows:
    """A ProtocolLedgerRowsReader that records its asks and answers a fixed set of rows."""

    def __init__(self, lines: Sequence[str] = (), fail: str = "") -> None:
        self._lines, self._fail = tuple(lines), fail
        self.asks: list[dict[str, object]] = []

    def rows(
        self,
        *,
        reader: str,
        window_hours: int,
        kinds: Sequence[str] = (),
        lanes: Sequence[str] = (),
    ) -> tuple[str, ...]:
        self.asks.append(
            {
                "reader": reader,
                "window_hours": window_hours,
                "kinds": tuple(kinds),
                "lanes": tuple(lanes),
            }
        )
        if self._fail:
            raise LedgerReadError(self._fail)
        return self._lines


@pytest.fixture(autouse=True)
def _no_ledger_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ledger path variable, and any file open or read through pathlib is a failure."""
    for name in [n for n in os.environ if n.endswith("LEDGER_PATH")]:
        monkeypatch.delenv(name)

    def _refuse(*_a: object, **_k: object) -> None:
        raise AssertionError("a lab-fill ledger port opened a file")

    monkeypatch.setattr(Path, "open", _refuse)
    monkeypatch.setattr(Path, "read_text", _refuse)


def test_status_row_time_reads_the_reader_for_this_fires_row_only() -> None:
    stub = _StubRows(_ROWS)
    reader = LocalLedgerReader(rows=stub)
    assert reader.status_row_time("2026-10-09T0100Z") == "2026-10-09T01:00:05Z"
    assert reader.status_row_time("2026-10-09T0200Z") is None  # another lane's row
    assert reader.status_row_time("2026-10-09T0300Z") is None  # not a STATUS row
    assert stub.asks[0] == {
        "reader": LAB_FILL_PORTS_READER,
        "window_hours": LAB_FILL_PORTS_WINDOW_HOURS,
        "kinds": ("STATUS",),
        "lanes": ("lab-fill",),
    }


def test_status_row_time_names_an_unread_ledger_never_an_empty_one() -> None:
    reader = LocalLedgerReader(rows=_StubRows(fail="reader exited 69: unreadable"))
    with pytest.raises(LabFillPortError, match="ledger unreadable"):
        reader.status_row_time("k")


def test_newest_cloud_claim_is_the_newest_host_cloud_claim_row() -> None:
    rows = (
        "2026-11-09T10:00:00Z | CLAIM | lane=a | host=cloud | ticket=OMN-1",
        "2026-11-09T12:30:00Z | CLAIM | lane=b | host=cloud | ticket=OMN-2",
        "2026-11-09T13:00:00Z | CLAIM | lane=c | host=h201 | ticket=OMN-3",
    )
    stub = _StubRows(rows)
    assert LocalLedgerReader(rows=stub).newest_cloud_claim() == "2026-11-09T12:30:00Z"
    assert stub.asks[0]["kinds"] == ("CLAIM",)
    assert stub.asks[0]["reader"] == LAB_FILL_PORTS_READER
    assert LocalLedgerReader(rows=_StubRows(rows[2:])).newest_cloud_claim() is None
    with pytest.raises(LabFillPortError, match="ledger unreadable"):
        LocalLedgerReader(rows=_StubRows(fail="x")).newest_cloud_claim()


def test_built_reports_read_the_terminal_rows_of_the_ticket_from_the_reader() -> None:
    rows = (
        "2026-11-09T14:29:11Z | TERMINAL | lane=fill-ticket-OMN-9-x | ticket=OMN-9 | outcome=skipped-already-built | falsifiers=pass | evidence=ran it",
        "2026-11-09T14:30:00Z | TERMINAL | lane=fill-ticket-OMN-8-x | ticket=OMN-8 | outcome=skipped-already-built | falsifiers=pass",
    )
    stub = _StubRows(rows)
    work = LocalMergedWork(
        reader=LocalOwnerReader(),
        rows=stub,
    )
    reports = work.built_reports("OMN-9")
    assert [(r.ticket, r.falsifiers_passed, r.evidence) for r in reports] == [
        ("OMN-9", True, "ran it")
    ]
    assert stub.asks[0]["kinds"] == ("TERMINAL",)
    assert stub.asks[0]["reader"] == LAB_FILL_PORTS_READER
    with pytest.raises(LabFillPortError, match="ledger unreadable"):
        LocalMergedWork(
            reader=LocalOwnerReader(),
            rows=_StubRows(fail="x"),
        ).built_reports("OMN-9")


def test_the_default_rows_reader_is_the_subprocess_reader_of_the_bus_node() -> None:
    assert isinstance(LocalLedgerReader()._rows, SubprocessLedgerRowsReader)
    assert isinstance(
        LocalMergedWork(reader=LocalOwnerReader())._rows,
        SubprocessLedgerRowsReader,
    )
