# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_ledger_rows_loader_effect``: the rows of a window of the ledger.

Reads the ``rows`` answer through ``node_work_ledger_reader_effect`` (the bus-fed projection),
decides with ``HandlerRowsLoader`` and prints the rows on stdout and one ``ROWS-LOADER`` line on
stderr (``source=file-fallback`` and the running ``fallback_count`` when the file's rows answered).
The caller names the live ledger file with ``--file-ledger`` only as the fallback the overlay
allows. Exit 0: the node answered. Exit 10: the file fallback answered (counted). Exit 69:
unreadable; no rows are printed and a retired fallback is never read.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from omnimarket.models.model_work_ledger_reader import (
    EnumReaderQuery,
    ModelReaderRequest,
)
from omnimarket.nodes.node_ledger_rows_loader_effect.adapters import (
    FileRowsReader,
    InMemoryRowsLoaderSink,
    JournalRowsLoaderSink,
)
from omnimarket.nodes.node_ledger_rows_loader_effect.handlers import HandlerRowsLoader
from omnimarket.nodes.node_ledger_rows_loader_effect.models import (
    EnumRowsSource,
    EnumRowsStatus,
    ModelRowsLoaderRequest,
)
from omnimarket.nodes.node_ledger_rows_loader_effect.overlay import load_overlay
from omnimarket.nodes.node_ledger_rows_loader_effect.protocols import (
    ProtocolFileRows,
    ProtocolRowsLoaderEventSink,
)
from omnimarket.nodes.node_work_ledger_reader_effect.adapters import (
    InMemoryEventSink,
    build_point_read,
)
from omnimarket.nodes.node_work_ledger_reader_effect.handlers import (
    HandlerWorkLedgerReader,
)
from omnimarket.nodes.node_work_ledger_reader_effect.overlay import (
    load_overlay as load_reader_overlay,
)
from omnimarket.nodes.node_work_ledger_reader_effect.protocols import (
    ProtocolLedgerPointRead,
)

EXIT_NODE = 0
EXIT_FILE_FALLBACK = 10
EXIT_UNREADABLE = 69


def exit_code(status: EnumRowsStatus, source: EnumRowsSource) -> int:
    if status is not EnumRowsStatus.OK:
        return EXIT_UNREADABLE
    return EXIT_NODE if source is EnumRowsSource.NODE else EXIT_FILE_FALLBACK


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    point_read: ProtocolLedgerPointRead | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    parser = argparse.ArgumentParser(prog="node_ledger_rows_loader_effect")
    parser.add_argument("--reader", required=True)
    parser.add_argument("--window-hours", required=True, type=int)
    parser.add_argument(
        "--file-ledger",
        help="the live ledger file; its rows (and its archive splits) are the fallback only",
    )
    parser.add_argument(
        "--archive-glob",
        help="glob of the archive split files beside the ledger file (under archive/); none reads the live file only",
    )
    args = parser.parse_args(argv)
    env = os.environ if environ is None else environ
    reader_overlay = load_reader_overlay(env)
    overlay = load_overlay(env)
    reader = HandlerWorkLedgerReader(
        reader_overlay,
        point_read
        if point_read is not None
        else build_point_read(reader_overlay.route, env),
        InMemoryEventSink(),
        clock=clock,
    )
    answer = reader.handle(
        ModelReaderRequest(
            reader=args.reader,
            query=EnumReaderQuery.ROWS,
            window_hours=args.window_hours,
        )
    )
    journal = env.get(overlay.journal_env, "").strip()
    sink: ProtocolRowsLoaderEventSink = (
        JournalRowsLoaderSink(Path(journal)) if journal else InMemoryRowsLoaderSink()
    )
    file_rows: ProtocolFileRows | None = (
        FileRowsReader(
            Path(args.file_ledger),
            clock() - timedelta(hours=args.window_hours),
            args.archive_glob,
        )
        if args.file_ledger
        else None
    )
    result = HandlerRowsLoader(overlay, sink, file_rows, clock=clock).handle(
        ModelRowsLoaderRequest(
            reader=args.reader, window_hours=args.window_hours, node_answer=answer
        )
    )
    sys.stdout.write("".join(f"{line}\n" for line in result.lines))
    sys.stderr.write(result.log_line() + "\n")
    return exit_code(result.status, result.source)
