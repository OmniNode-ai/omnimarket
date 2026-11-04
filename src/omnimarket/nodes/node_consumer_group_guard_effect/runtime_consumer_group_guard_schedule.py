# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Command line for the consumer-group guard's schedule verbs."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Never

import yaml
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError

from .guard_cutover import verify_cutover
from .guard_schedule import GuardScheduler, load_overlay, load_schedule
from .guard_schedule_models import ModelGuardScheduleResult

VERBS = ("check", "install", "status", "run-now", "uninstall")


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise ValueError(f"unknown argument: {message}")


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="schedule", description=__doc__)
    verbs = parser.add_subparsers(dest="verb", required=True, parser_class=_Parser)
    verbs.add_parser("check")
    for verb in VERBS[1:]:
        sub = verbs.add_parser(verb)
        sub.add_argument("--host", required=True, help="the host's name in the overlay")
        if verb in ("install", "status"):
            sub.add_argument(
                "--omni-home",
                dest="workspace_root",
                default=os.environ.get("OMNI_HOME", ""),
            )
        if verb == "install":
            sub.add_argument("--dry-run", action="store_true")
    cutover = verbs.add_parser("verify-cutover")
    cutover.add_argument("--host", required=True)
    cutover.add_argument("--evidence", required=True, type=Path)
    return parser


def main(argv: Sequence[str]) -> int:
    try:
        args = _parser().parse_args(list(argv))
    except ValueError as exc:
        sys.stderr.write(f"INDETERMINATE: {exc}\n")
        return 3
    try:
        if args.verb == "verify-cutover":
            result = verify_cutover(
                json.loads(args.evidence.read_text(encoding="utf-8")),
                host=args.host,
                interval_seconds=load_schedule().cadence.interval_seconds,
            )
            sys.stdout.write(result.stdout)
            sys.stderr.write(result.stderr)
            return result.exit_code
        scheduler = GuardScheduler(
            schedule=load_schedule(),
            overlay=load_overlay(),
            home=Path.home(),
            dispatch_venv=(
                Path(os.environ["ONEX_DISPATCH_VENV"])
                if os.environ.get("ONEX_DISPATCH_VENV")
                else None
            ),
        )
        result = _dispatch(scheduler, args)
    except (
        NodeOverlayError,
        ValidationError,
        ValueError,
        OSError,
        yaml.YAMLError,
    ) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        return 3
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.exit_code


def _dispatch(
    scheduler: GuardScheduler, args: argparse.Namespace
) -> ModelGuardScheduleResult:
    if args.verb == "check":
        return scheduler.check()
    if args.verb in ("install", "status"):
        if not args.workspace_root:
            raise ValueError("OMNI_HOME is not set and --omni-home was not passed")
        if args.verb == "install":
            return scheduler.install(
                args.host, omni_root=Path(args.workspace_root), dry_run=args.dry_run
            )
        return scheduler.status(args.host, omni_root=Path(args.workspace_root))
    if args.verb == "run-now":
        return scheduler.run_now(args.host)
    return scheduler.uninstall(args.host)
