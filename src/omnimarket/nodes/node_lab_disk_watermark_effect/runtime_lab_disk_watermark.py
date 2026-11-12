# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Command line of the watermark node: ``run`` one reading, or a ``schedule`` verb.

``run`` exits 0 at ``ok`` and ``warn``, 2 at ``page`` (the service fails, the journal holds a PAGE line),
3 when the overlay or request is unusable and 4 when the host disk reading ended in its typed failure event (the
reading file is already written, and the journal holds a READING-FAILED line naming the reason).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Never

import yaml
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.model_host_disk_reading import (
    ModelHostDiskReadFailed,
)
from omnimarket.nodes.node_lab_disk_watermark_effect.handlers.handler_lab_disk_watermark import (
    HandlerLabDiskWatermark,
    WatermarkRefusalError,
    host_row,
    load_overlay,
    load_schedule,
)
from omnimarket.nodes.node_lab_disk_watermark_effect.models.model_lab_disk_watermark import (
    ModelLabDiskWatermarkRequest,
    ModelLabDiskWatermarkScheduleResult,
)
from omnimarket.nodes.node_lab_disk_watermark_effect.watermark_schedule import (
    WatermarkScheduler,
)

EXIT_PAGE = 2
EXIT_INDETERMINATE = 3
EXIT_READING_FAILED = 4


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise ValueError(f"unknown argument: {message}")


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="node_lab_disk_watermark_effect", description=__doc__)
    commands = parser.add_subparsers(
        dest="command", required=True, parser_class=_Parser
    )
    run = commands.add_parser("run")
    run.add_argument("--host", required=True)
    run.add_argument("--overlay", default="")
    run.add_argument("--dry-run", action="store_true")
    verbs = commands.add_parser("schedule").add_subparsers(
        dest="verb", required=True, parser_class=_Parser
    )
    verbs.add_parser("check").add_argument("--overlay", default="")
    install = verbs.add_parser("install")
    install.add_argument("--host", required=True)
    install.add_argument("--python", type=Path, default=Path(sys.executable))
    install.add_argument("--source-root", required=True, type=Path)
    install.add_argument("--overlay", required=True, type=Path)
    install.add_argument("--workspace-root", required=True, type=Path)
    install.add_argument("--dry-run", action="store_true")
    for verb in ("status", "run-now", "uninstall"):
        verbs.add_parser(verb)
    return parser


def main(argv: Sequence[str]) -> int:
    try:
        args = _parser().parse_args(list(argv))
        if args.command == "run":
            return _run(args)
        return _schedule(args)
    except (
        NodeOverlayError,
        ValidationError,
        WatermarkRefusalError,
        ValueError,
        OSError,
        yaml.YAMLError,
    ) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        return EXIT_INDETERMINATE


def _run(args: argparse.Namespace) -> int:
    reading = HandlerLabDiskWatermark().handle(
        ModelLabDiskWatermarkRequest(
            host_name=args.host, overlay_file=args.overlay, dry_run=args.dry_run
        )
    )
    sys.stdout.write(reading.model_dump_json() + "\n")
    if reading.paged:
        worst = max(reading.volumes, key=lambda v: v.used_percent)
        sys.stderr.write(
            f"PAGE {reading.host_name}: {worst.path} {worst.used_percent}% used, "
            f"page mark {worst.page_percent}%, {worst.free_bytes} bytes free\n"
        )
        return EXIT_PAGE
    if isinstance(reading.host_disk, ModelHostDiskReadFailed):
        failed = reading.host_disk
        sys.stderr.write(
            f"READING-FAILED {failed.host_name}: {failed.reason}: {failed.detail}\n"
        )
        return EXIT_READING_FAILED
    return 0


def _schedule(args: argparse.Namespace) -> int:
    if args.verb == "check":
        overlay = load_overlay(args.overlay)
        for row in overlay.hosts:
            host_row(overlay, row.name)
        sys.stdout.write(f"OK {len(overlay.hosts)} hosts, each with a watermark row\n")
        return 0
    schedule = load_schedule()
    if args.verb == "install":
        overlay = load_overlay(str(args.overlay))
        host_row(overlay, args.host)
        if overlay.reading is not None:
            # the reading's interval is the overlay's: a value of the host's reading, never of the node
            schedule = schedule.model_copy(
                update={"interval_seconds": overlay.reading.interval_seconds}
            )
    scheduler = WatermarkScheduler(schedule=schedule, home=Path.home())
    result: ModelLabDiskWatermarkScheduleResult
    if args.verb == "install":
        result = scheduler.install(
            args.host,
            python=args.python,
            source_root=args.source_root,
            overlay_file=args.overlay,
            workspace_root=args.workspace_root,
            dry_run=args.dry_run,
        )
    elif args.verb == "status":
        result = scheduler.status()
    elif args.verb == "run-now":
        result = scheduler.run_now()
    else:
        result = scheduler.uninstall()
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.exit_code
