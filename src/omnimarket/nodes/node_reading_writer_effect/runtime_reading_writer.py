# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_reading_writer_effect <verb>``.

``hourly``   probe the latest closed window and append the reading rows (what the timer runs).
``probe``    the same reads and computation, appending nothing.
``schedule`` check | install | uninstall | status | run-now, for a host in the overlay.

Exit codes: 0 every reading was computed and written (a piece reading NOT WORKING is still 0:
a probe changes nothing and refuses nothing); 1 the run or a row failed (a failed window, a
conflicting duplicate, a replay failure, an unreadable source, an append that did not land);
3 a deployment fact is missing (INDETERMINATE).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError, find_node_overlay
from omnimarket.models.model_reading import (
    EnumReadingOutcome,
    ModelReadingWindowCommand,
)
from omnimarket.nodes.node_reading_compute.handlers.handler_reading_compute import (
    stamp_text,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.landing_adapters import (
    WatcherFileLandingSource,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
    build_appender,
    build_row_source,
)
from omnimarket.nodes.node_reading_writer_effect.handlers.handler_reading_writer import (
    HandlerReadingWriter,
    window_for,
)
from omnimarket.nodes.node_reading_writer_effect.handlers.schedule import (
    ReadingScheduler,
    load_schedule,
)
from omnimarket.nodes.node_reading_writer_effect.overlay import NODE_NAME, load_overlay
from omnimarket.nodes.node_two_backend_suite_effect.adapters.report_sources import (
    FileReportSource,
)


def _stamp_arg(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def _run_window(arguments: argparse.Namespace, write: bool) -> int:
    overlay = load_overlay()
    source = build_row_source(overlay.ledger_read)
    appender = build_appender(overlay.ledger_append, overlay.publish.lane)
    now = datetime.now(UTC)
    start = (
        _stamp_arg(arguments.window_start)
        if arguments.window_start
        else window_for(
            now, overlay.window.length_seconds, overlay.window.close_delay_seconds
        )[0]
    )
    two_backend = overlay.two_backend
    report_path = os.environ.get(two_backend.report_path_env) if two_backend else None
    handler = HandlerReadingWriter(
        overlay=overlay,
        source=source,
        appender=appender,
        clock=lambda: datetime.now(UTC),
        landing=(
            WatcherFileLandingSource(overlay.landing_route, os.environ)
            if overlay.landing_route is not None
            else None
        ),
        two_backend_source=FileReportSource(Path(report_path)) if report_path else None,
    )
    result = handler.handle(
        ModelReadingWindowCommand(
            correlation_id=f"reading-{stamp_text(start)}",
            window_start=start,
            pieces=tuple(arguments.piece or ()),
            write=write,
        )
    )
    if result.status == "failed":
        sys.stderr.write(f"READING-WINDOW-FAILED {result.failure}\n")
        return 1
    for piece in result.pieces:
        sys.stdout.write(
            f"READING piece={piece.piece} verdict={piece.verdict.value} "
            f"outcome={piece.outcome.value} ledger={piece.ledger} key={piece.row_key} "
            f"reason={','.join(piece.reasons)}\n"
        )
    broken = [
        piece
        for piece in result.pieces
        if piece.outcome is not EnumReadingOutcome.READ
        or piece.ledger.startswith("append-failed")
    ]
    sys.stdout.write(
        f"WINDOW start={stamp_text(result.window_start or start)} pieces={len(result.pieces)} "
        f"failed={len(broken)} write={'yes' if write else 'no'}\n"
    )
    return 1 if broken else 0


def _schedule(arguments: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="schedule")
    verbs = parser.add_subparsers(dest="verb", required=True)
    verbs.add_parser("check")
    for verb in ("install", "uninstall", "status", "run-now"):
        sub = verbs.add_parser(verb)
        sub.add_argument("--host", required=True, help="the host's name in the overlay")
        if verb == "install":
            sub.add_argument(
                "--workspace-root",
                dest="workspace_root",
                default=os.environ.get("OMNI_HOME", ""),
            )
            sub.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(list(arguments))
    scheduler = ReadingScheduler(
        schedule=load_schedule(), overlay=load_overlay(), home=Path.home()
    )
    if args.verb == "check":
        result = scheduler.check()
    elif args.verb == "install":
        if not args.workspace_root:
            raise ValueError("OMNI_HOME is not set and --workspace-root was not passed")
        found = find_node_overlay(NODE_NAME)
        if found is None:
            raise NodeOverlayError(
                "the overlay file cannot be found to pin its root in the unit"
            )
        result = scheduler.install(
            args.host,
            omni_root=Path(args.workspace_root),
            overlay_root=found.parent.parent,
            dry_run=args.dry_run,
        )
    elif args.verb == "uninstall":
        result = scheduler.uninstall(args.host)
    elif args.verb == "status":
        result = scheduler.status(args.host)
    else:
        result = scheduler.run_now(args.host)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.exit_code


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if arguments and arguments[0] == "schedule":
            return _schedule(arguments[1:])
        parser = argparse.ArgumentParser(prog=NODE_NAME, description=__doc__)
        parser.add_argument("verb", choices=("hourly", "probe"))
        parser.add_argument(
            "--window-start", default="", help="a closed window's start, UTC"
        )
        parser.add_argument("--piece", type=int, action="append")
        args = parser.parse_args(arguments)
        return _run_window(args, write=args.verb == "hourly")
    except (
        NodeOverlayError,
        LedgerRouteError,
        ValidationError,
        ValueError,
        OSError,
    ) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
