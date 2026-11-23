# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_work_ledger_reader_effect``: one read by one named reader.

Prints the query node's verb lines on stdout and one ``READER`` summary line on stderr.
Exit 0: answered within the freshness bound. Exit 1: answered, but stale (the lines are
printed). Exit 3: the reader is not moved; the caller keeps its own read. Exit 69: unreadable;
nothing was read, and the file is not read in its place.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from omnimarket.nodes.node_work_ledger_reader_effect.adapters import (
    InMemoryEventSink,
    JournalEventSink,
    build_point_read,
)
from omnimarket.nodes.node_work_ledger_reader_effect.handlers import (
    HandlerWorkLedgerReader,
)
from omnimarket.nodes.node_work_ledger_reader_effect.handlers.detail_capture_bound import (
    bound_capture,
    load_detail_capture_settings,
)
from omnimarket.nodes.node_work_ledger_reader_effect.models import (
    EnumReaderQuery,
    EnumReaderStatus,
    ModelReaderOverlay,
    ModelReaderRequest,
)
from omnimarket.nodes.node_work_ledger_reader_effect.models.model_detail_capture import (
    ModelDetailCaptureSettings,
)
from omnimarket.nodes.node_work_ledger_reader_effect.overlay import load_overlay
from omnimarket.nodes.node_work_ledger_reader_effect.protocols import (
    ProtocolLedgerPointRead,
    ProtocolReaderEventSink,
)

EXIT = {
    EnumReaderStatus.OK: 0,
    EnumReaderStatus.STALE: 1,
    EnumReaderStatus.NOT_MOVED: 3,
    EnumReaderStatus.UNREADABLE: 69,
}


def build_sink(
    overlay: ModelReaderOverlay, environ: Mapping[str, str]
) -> ProtocolReaderEventSink:
    path = environ.get(overlay.events.journal_env, "").strip()
    return JournalEventSink(Path(path)) if path else InMemoryEventSink()


def write_bounded_lines(
    lines: Sequence[str], settings: ModelDetailCaptureSettings
) -> None:
    """Print the verb lines on stdout through the contract's detail capture bound.

    A read past ``max_bytes`` prints its first ``head_lines``, its last ``tail_lines`` and one
    line stating the dropped line count; a smaller read prints unchanged.
    """
    with tempfile.TemporaryDirectory(prefix="reader-detail-") as scratch:
        dest = Path(scratch) / "stdout.detail"
        bound_capture((f"{line}\n".encode() for line in lines), dest, settings)
        sys.stdout.write(dest.read_text(encoding="utf-8"))


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    point_read: ProtocolLedgerPointRead | None = None,
    detail_settings: ModelDetailCaptureSettings | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="node_work_ledger_reader_effect")
    parser.add_argument("--reader", required=True)
    parser.add_argument(
        "--query", required=True, choices=[q.value for q in EnumReaderQuery]
    )
    parser.add_argument("--lane")
    parser.add_argument("--ticket")
    parser.add_argument("--parity", action="store_true")
    parser.add_argument("--window-hours", type=int)
    parser.add_argument(
        "--kind",
        action="append",
        default=[],
        help="a rows read only: a ledger row type to keep (repeat for several)",
    )
    parser.add_argument(
        "--row-lane",
        action="append",
        default=[],
        help="a rows read only: keep the rows of this lane (repeat for several)",
    )
    parser.add_argument(
        "--term",
        help="a rows read only: a case-insensitive text the row's first line holds",
    )
    args = parser.parse_args(argv)
    env = os.environ if environ is None else environ
    overlay = load_overlay(env)
    handler = HandlerWorkLedgerReader(
        overlay,
        point_read if point_read is not None else build_point_read(overlay.route, env),
        build_sink(overlay, env),
    )
    result = handler.handle(
        ModelReaderRequest(
            reader=args.reader,
            query=EnumReaderQuery(args.query),
            lane=args.lane,
            ticket=args.ticket,
            read_parity=args.parity,
            window_hours=args.window_hours,
            kinds=tuple(args.kind),
            lanes=tuple(args.row_lane),
            term=args.term,
        )
    )
    write_bounded_lines(
        result.lines,
        detail_settings
        if detail_settings is not None
        else load_detail_capture_settings(),
    )
    sys.stderr.write(result.summary() + "\n")
    return EXIT[result.status]
