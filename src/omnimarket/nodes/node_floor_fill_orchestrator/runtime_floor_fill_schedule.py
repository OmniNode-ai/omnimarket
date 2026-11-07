# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m <runtime module> schedule <install|uninstall|status|run-now> --this-host <host>``.

Reads the overlay's ``floor_fill`` section from ``<root>/node_floor_fill_orchestrator/overlay.yaml`` under
``ONEX_SKILL_OVERLAY_ROOTS`` and the lab host table, then hands both to the schedule handler. A refused overlay or
host prints its refusal naming the key and exits 1.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from omnimarket.handlers.node_overlay_reader import find_node_overlay
from omnimarket.handlers.overlay_keys import resolve_lab_hosts_table
from omnimarket.nodes.node_floor_fill_orchestrator.handlers.adapter_floor_fill_schedule import (
    NODE_NAME,
    FloorFillScheduler,
    Runner,
    run_command,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models import (
    FloorFillConfigError,
    ModelFloorFillSchedule,
    ModelFloorFillScheduleResult,
    load_floor_fill_config,
    load_floor_fill_schedule,
)

OVERLAY_SECTION = "floor_fill"
VERBS = ("install", "uninstall", "status", "run-now")


def run_schedule(
    argv: Sequence[str],
    overlay: Mapping[str, Any],
    *,
    hosts: Iterable[tuple[str, bool]],
    home: Path,
    schedule: ModelFloorFillSchedule | None = None,
    run: Runner = run_command,
) -> ModelFloorFillScheduleResult:
    """Parse the verb, build the handler from the overlay and run it."""
    parser = argparse.ArgumentParser(prog="schedule")
    verbs = parser.add_subparsers(dest="verb", required=True)
    for verb in VERBS:
        sub = verbs.add_parser(verb)
        sub.add_argument("--this-host", required=True, help="the host this runs on")
        if verb == "install":
            sub.add_argument(
                "--workspace-root",
                dest="workspace_root",
                default=os.environ.get("OMNI_HOME", ""),
            )
            sub.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(list(argv))
    try:
        config = load_floor_fill_config(overlay.get(OVERLAY_SECTION, {}))
    except FloorFillConfigError as error:
        return ModelFloorFillScheduleResult(exit_code=1, stderr=f"{error}\n")
    handler = FloorFillScheduler(
        schedule=schedule or load_floor_fill_schedule(),
        config=config,
        hosts=hosts,
        home=home,
        run=run,
    )
    if args.verb == "install":
        if not args.workspace_root:
            return ModelFloorFillScheduleResult(
                exit_code=1,
                stderr="OMNI_HOME is not set and --workspace-root was not passed\n",
            )
        return handler.install(
            this_host=args.this_host,
            omni_root=Path(args.workspace_root),
            dry_run=args.dry_run,
        )
    if args.verb == "uninstall":
        return handler.uninstall(this_host=args.this_host)
    if args.verb == "status":
        return handler.status(this_host=args.this_host)
    return handler.run_now(this_host=args.this_host)


def main(argv: Sequence[str]) -> int:
    found = find_node_overlay(NODE_NAME)
    if found is None:
        sys.stderr.write(
            f"no overlay: {NODE_NAME}/overlay.yaml is not under ONEX_SKILL_OVERLAY_ROOTS\n"
        )
        return 1
    overlay = yaml.safe_load(found.read_text(encoding="utf-8"))
    table = resolve_lab_hosts_table()
    result = run_schedule(
        argv,
        overlay if isinstance(overlay, dict) else {},
        hosts=[(row.name, row.available) for row in table.hosts],
        home=Path.home(),
    )
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.exit_code
