# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Registry module tick main for the host thermal hold report (OMN-19998).

    python -m omnimarket.nodes.node_host_thermal_hold_report_effect.runtime_host_thermal_hold_tick --omni-home <root> --state-dir <dir>

The host installer renders every ``registry_module_tick`` unit as that command (the unit's own state directory holds
the episode file and ``last-run.json``, the unit heartbeat). ``--probe`` exits 0 after one status line without loading an
overlay or touching a file. Exit 0: nothing left undone. 3: a row was kept, refused or the alarm was not accepted, or a
guard file could not be used. 2: the overlay is refused, the host is unnamed or unlisted, or the append command is not set.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from dataclasses import asdict
from pathlib import Path

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.tick_unit_arguments import (
    add_tick_arguments,
    write_tick_heartbeat,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    CommandLedgerAppender,
)

from .adapters import FileEpisodeStore, GuardFileState
from .handlers import HandlerHostThermalHoldReport
from .models import ModelHostThermalHoldReportRequest
from .overlay import load_overlay

PROBE_FLAG = "--probe"
PROBE_STATUS = "probe_ok"
EXIT_OK, EXIT_UNDONE, EXIT_REFUSED = 0, 3, 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="omnimarket.nodes.node_host_thermal_hold_report_effect.runtime_host_thermal_hold_tick",
        description="One pass of the host thermal hold report",
    )
    parser.add_argument(
        PROBE_FLAG,
        action="store_true",
        help="Start check: exit 0 and print one status line without loading an overlay or writing a file",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="decide and send or move nothing"
    )
    parser.add_argument("--host", default=None, help="this host's name in the overlay")
    add_tick_arguments(parser)
    return parser


def _refuse(args: argparse.Namespace, detail: str) -> int:
    line: dict[str, object] = {"status": "refused", "detail": detail}
    write_tick_heartbeat(args.state_dir, code=EXIT_REFUSED, line=line)
    sys.stdout.write(json.dumps(line, sort_keys=True) + "\n")
    return EXIT_REFUSED


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.probe:
        sys.stdout.write(json.dumps({"status": PROBE_STATUS}, sort_keys=True) + "\n")
        return EXIT_OK
    try:
        overlay = load_overlay(os.environ)
    except (NodeOverlayError, ValidationError, ValueError) as exc:
        return _refuse(args, str(exc))
    host = (args.host or os.environ.get(overlay.host_env, "")).strip()
    if not host:
        return _refuse(args, f"{overlay.host_env} is not set and --host was not passed")
    try:
        row = overlay.host_row(host)
    except ValueError as exc:
        return _refuse(args, str(exc))
    command = shlex.split(os.environ.get(overlay.ledger_append.command_env, ""))
    if not command:
        return _refuse(
            args,
            f"{overlay.ledger_append.command_env} is not set; the report has no ledger route",
        )
    if args.state_dir is None:
        return _refuse(args, "--state-dir is required: the episode file lives there")
    ledger_host = os.environ.get(overlay.ledger_append.host_env, "").strip() or host
    handler = HandlerHostThermalHoldReport(
        GuardFileState(
            Path.home() / row.guard_state_dir,
            overlay.latch_file,
            overlay.last_file,
            overlay.outbox_file,
            overlay.refused_file,
            overlay.claim_suffix,
        ),
        FileEpisodeStore(args.state_dir, overlay.episode_file),
        CommandLedgerAppender(
            command, row.hold_lane, ledger_host, overlay.ledger_append.timeout_seconds
        ),
    )
    result = handler.handle(
        ModelHostThermalHoldReportRequest(
            host=host, overlay=overlay, dry_run=args.dry_run
        )
    )
    line = {"status": "ok" if result.ok else "undone", **asdict(result)}
    code = EXIT_OK if result.ok else EXIT_UNDONE
    if not args.dry_run:
        write_tick_heartbeat(args.state_dir, code=code, line=line)
    sys.stdout.write(json.dumps(line, sort_keys=True) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
