# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Argument adapter for the ``schedule`` verbs of the lane census node.

``python -m omnimarket.nodes.node_lane_census_effect schedule <verb>``: ``check`` fails for a host
row the lab host table says takes no Docker work (with ``--against-lab-table``) or a duplicate row;
``install`` and ``run-now`` act on the named host's row of the node overlay.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.overlay_keys import resolve_lab_hosts_table

from .handlers.adapter_lane_census_schedule import (
    LaneCensusScheduler,
    load_overlay,
    load_schedule,
)


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="schedule", description=__doc__)
    verbs = parser.add_subparsers(dest="verb", required=True)
    check = verbs.add_parser("check")
    check.add_argument(
        "--against-lab-table",
        action="store_true",
        help="also require every declared host to be an available lab host table row that takes Docker",
    )
    for verb in ("install", "run-now"):
        sub = verbs.add_parser(verb)
        sub.add_argument("--host", required=True, help="the host's name in the overlay")
        if verb == "install":
            sub.add_argument(
                "--omni-home",
                dest="workspace_root",
                default=os.environ.get("OMNI_HOME", ""),
            )
            sub.add_argument("--config-root", default="")
            sub.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(list(argv))
    try:
        scheduler = LaneCensusScheduler(
            schedule=load_schedule(), overlay=load_overlay(), home=Path.home()
        )
        if args.verb == "check":
            result = scheduler.check(
                resolve_lab_hosts_table() if args.against_lab_table else None
            )
        elif args.verb == "install":
            if not args.workspace_root:
                raise ValueError("OMNI_HOME is not set and --omni-home was not passed")
            result = scheduler.install(
                args.host,
                omni_root=Path(args.workspace_root),
                config_root=Path(args.config_root) if args.config_root else None,
                dry_run=args.dry_run,
            )
        else:
            result = scheduler.run_now(args.host)
    except (NodeOverlayError, ValidationError, ValueError, OSError) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        return 3
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.exit_code
