# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Argument adapter for the ``schedule`` verbs of the host reconcile node.

``python -m omnimarket.nodes.node_host_reconcile_effect schedule <verb>``: ``check`` fails for any
host that declares the reconciler with no scheduler; ``install``, ``status``, ``run-now`` and
``uninstall`` act on the named host's row of the node overlay.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Never

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.overlay_keys import resolve_lab_hosts_table
from omnimarket.models.model_host_reconcile_schedule import (
    ModelHostReconcileScheduleResult,
)

from .adapter_host_reconcile_schedule import (
    HostReconcileScheduler,
    load_overlay,
    load_schedule,
)

VERBS = ("check", "install", "status", "run-now", "uninstall")


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise ValueError(f"unknown argument: {message}")


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="schedule", description=__doc__)
    verbs = parser.add_subparsers(dest="verb", required=True, parser_class=_Parser)
    check = verbs.add_parser("check")
    check.add_argument(
        "--against-lab-table",
        action="store_true",
        help="also require every available host of the lab host table to declare the reconciler",
    )
    for verb in VERBS[1:]:
        sub = verbs.add_parser(verb)
        sub.add_argument("--host", required=True, help="the host's name in the overlay")
        if verb == "install":
            sub.add_argument(
                "--omni-home",
                dest="workspace_root",
                default=os.environ.get("OMNI_HOME", ""),
            )
            sub.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str]) -> int:
    try:
        args = _parser().parse_args(list(argv))
    except ValueError as exc:
        sys.stderr.write(f"INDETERMINATE: {exc}\n")
        return 3
    try:
        handler = HostReconcileScheduler(
            schedule=load_schedule(),
            overlay=load_overlay(),
            home=Path.home(),
            dispatch_venv=(
                Path(os.environ["ONEX_DISPATCH_VENV"])
                if os.environ.get("ONEX_DISPATCH_VENV")
                else None
            ),
        )
        result = _dispatch(handler, args)
    except (NodeOverlayError, ValidationError, ValueError, OSError) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        return 3
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.exit_code


def _dispatch(
    handler: HostReconcileScheduler, args: argparse.Namespace
) -> ModelHostReconcileScheduleResult:
    if args.verb == "check":
        return handler.check(
            resolve_lab_hosts_table() if args.against_lab_table else None
        )
    if args.verb == "install":
        if not args.workspace_root:
            raise ValueError("OMNI_HOME is not set and --omni-home was not passed")
        return handler.install(
            args.host, omni_root=Path(args.workspace_root), dry_run=args.dry_run
        )
    if args.verb == "status":
        return handler.status(args.host)
    if args.verb == "run-now":
        return handler.run_now(args.host)
    return handler.uninstall(args.host)
