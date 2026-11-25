# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m <runtime module> schedule <install|uninstall|status|run-now> --this-host <host>``.

Reads the overlay's ``floor_fill`` section from ``<root>/node_floor_fill_orchestrator/overlay.yaml`` under the resolver's
ordered roots (the settings store's root first, then the overlays clone, then ``ONEX_SKILL_OVERLAY_ROOTS``) and the lab
host table, then hands both to the schedule handler, so a key set in the store reaches the unit the next install renders. ``install --profile live``
also reads the overlay's ``floor_fill_live`` section (refused, naming the key, when it is missing or unknown). A
refused overlay or host prints its refusal naming the key and exits 1.
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
from omnimarket.handlers.node_overlay_resolved_roots import resolved_overlay_environ
from omnimarket.handlers.overlay_keys import resolve_lab_hosts_table
from omnimarket.models.overlay_keys.model_overlay_documents import ModelLabHostRow
from omnimarket.nodes.node_floor_fill_orchestrator.handlers.adapter_floor_fill_schedule import (
    NODE_NAME,
    PROFILES,
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
from omnimarket.nodes.node_floor_fill_orchestrator.models.model_floor_fill_live_settings import (
    LIVE_OVERLAY_SECTION,
    load_floor_fill_live_settings,
)

OVERLAY_SECTION = "floor_fill"
VERBS = ("install", "uninstall", "status", "run-now")


def run_schedule(
    argv: Sequence[str],
    overlay: Mapping[str, Any],
    *,
    hosts: Iterable[tuple[str, bool]],
    home: Path,
    host_rows: Iterable[ModelLabHostRow] = (),
    environment: Mapping[str, str] | None = None,
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
            sub.add_argument("--profile", choices=PROFILES, default=PROFILES[0])
    args = parser.parse_args(list(argv))
    try:
        config = load_floor_fill_config(overlay.get(OVERLAY_SECTION, {}))
    except FloorFillConfigError as error:
        return ModelFloorFillScheduleResult(exit_code=1, stderr=f"{error}\n")
    live = None
    if args.verb == "install" and args.profile == "live":
        try:
            live = load_floor_fill_live_settings(overlay.get(LIVE_OVERLAY_SECTION, {}))
        except FloorFillConfigError as error:
            return ModelFloorFillScheduleResult(
                exit_code=1, stderr=f"{', '.join(error.keys) or error}\n"
            )
    handler = FloorFillScheduler(
        schedule=schedule or load_floor_fill_schedule(),
        config=config,
        hosts=hosts,
        home=home,
        run=run,
        live=live,
        host_rows=host_rows,
        environment=environment,
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


def load_schedule_overlay(
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """The floor fill overlay from the first resolver root that holds one (the store's root first), or ``None``."""
    found = find_node_overlay(NODE_NAME, resolved_overlay_environ(environ))
    if found is None:
        return None
    loaded = yaml.safe_load(found.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


def main(argv: Sequence[str]) -> int:
    overlay = load_schedule_overlay()
    if overlay is None:
        sys.stderr.write(
            f"no overlay: {NODE_NAME}/overlay.yaml is not under the settings store's root or ONEX_SKILL_OVERLAY_ROOTS\n"
        )
        return 1
    table = resolve_lab_hosts_table()
    result = run_schedule(
        argv,
        overlay,
        hosts=[(row.name, row.available) for row in table.hosts],
        host_rows=table.hosts,
        environment=resolved_overlay_environ(),
        home=Path.home(),
    )
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.exit_code
