# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Command line of the retention node: ``run`` one pass, or a ``schedule`` verb."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Never

import yaml
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_temp_retention_effect.handlers.handler_temp_retention import (
    HandlerTempRetention,
    RefusalError,
    host_row,
    load_bounds,
    load_overlay,
    load_schedule,
    validate_declarations,
)
from omnimarket.nodes.node_temp_retention_effect.models.model_temp_retention import (
    ModelRetentionScheduleResult,
)
from omnimarket.nodes.node_temp_retention_effect.models.model_temp_retention_request import (
    ModelTempRetentionRequest,
)
from omnimarket.nodes.node_temp_retention_effect.retention_schedule import (
    RetentionScheduler,
)

VERBS = ("check", "install", "status", "run-now", "uninstall")
NODE_DIR = Path(__file__).resolve().parent
EXIT_INDETERMINATE = 3


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise ValueError(f"unknown argument: {message}")


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="node_temp_retention_effect", description=__doc__)
    commands = parser.add_subparsers(
        dest="command", required=True, parser_class=_Parser
    )
    run = commands.add_parser("run")
    run.add_argument("--host", required=True, help="the host's name in the overlay")
    run.add_argument(
        "--overlay", default="", help="the overlay file (default: the environment pin)"
    )
    run.add_argument("--dry-run", action="store_true")
    schedule = commands.add_parser("schedule")
    verbs = schedule.add_subparsers(dest="verb", required=True, parser_class=_Parser)
    verbs.add_parser("check").add_argument("--overlay", default="")
    for verb in VERBS[1:]:
        sub = verbs.add_parser(verb)
        sub.add_argument("--host", required=True)
        if verb != "install":
            sub.add_argument(
                "--overlay",
                default="",
                help="the overlay file (default: the environment pin)",
            )
        if verb == "install":
            sub.add_argument(
                "--python",
                type=Path,
                default=Path(sys.executable),
                help="the interpreter the timer runs (default: the one running this command)",
            )
            sub.add_argument(
                "--source-root",
                required=True,
                type=Path,
                help="the checkout that holds src/",
            )
            sub.add_argument(
                "--overlay",
                required=True,
                type=Path,
                help="the overlay file the timer will read",
            )
            sub.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str]) -> int:
    try:
        args = _parser().parse_args(list(argv))
    except ValueError as exc:
        sys.stderr.write(f"INDETERMINATE: {exc}\n")
        return EXIT_INDETERMINATE
    try:
        if args.command == "run":
            return _run(args)
        return _schedule(args)
    except (
        NodeOverlayError,
        ValidationError,
        RefusalError,
        ValueError,
        OSError,
        yaml.YAMLError,
    ) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        return EXIT_INDETERMINATE


def _run(args: argparse.Namespace) -> int:
    result = HandlerTempRetention().handle(
        ModelTempRetentionRequest(
            host_name=args.host, overlay_file=args.overlay, dry_run=args.dry_run
        )
    )
    sys.stdout.write(result.model_dump_json() + "\n")
    return 1 if result.errors or result.refused else 0


def _schedule(args: argparse.Namespace) -> int:
    overlay_file = getattr(args, "overlay", "") or ""
    overlay = load_overlay(str(overlay_file))
    scheduler = RetentionScheduler(
        schedule=load_schedule(), rows=overlay.hosts, home=Path.home()
    )
    if args.verb == "check":
        bounds = load_bounds()
        for row in overlay.hosts:
            host_row(overlay, row.name)
            validate_declarations(row, bounds)
        sys.stdout.write(f"OK {len(overlay.hosts)} hosts, each with a retention row\n")
        return 0
    result: ModelRetentionScheduleResult
    if args.verb == "install":
        result = scheduler.install(
            args.host,
            python=args.python,
            source_root=args.source_root,
            overlay_file=args.overlay,
            dry_run=args.dry_run,
        )
    elif args.verb == "status":
        result = scheduler.status(args.host)
    elif args.verb == "run-now":
        result = scheduler.run_now(args.host)
    else:
        result = scheduler.uninstall(args.host)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.exit_code
