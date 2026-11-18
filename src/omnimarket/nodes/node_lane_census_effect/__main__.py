# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the lane census in process on the host named by its local timer."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from pathlib import Path
from typing import Never

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError, find_node_overlay
from omnimarket.models.model_lane_census import ModelLaneCensusCommand
from omnimarket.nodes.node_compose_project_census_compute.overlay import (
    NODE_NAME as COMPOSE_CENSUS_NODE,
)
from omnimarket.nodes.node_compose_project_census_compute.overlay import load_policy

from .handlers.adapter_compose_project_check import (
    ComposeProjectCheckStep,
    ModelComposeProjectCheckCommand,
)
from .handlers.adapter_lane_census_schedule import load_schedule
from .handlers.handler_lane_census_effect import HandlerLaneCensusEffect

# What the census exit code says when the per-project step found drift or could not read.
DRIFT_EXIT = 30
HOST_UNDECLARED_EXIT = 5
INVENTORY_UNOBSERVABLE_EXIT = 4


def _project_handler() -> ComposeProjectCheckStep:
    return ComposeProjectCheckStep()


def _combined_exit(census_exit: int, outcome: str) -> int:
    """The census's own non-zero code stands; the project step adds to a clean or drifted census only."""
    if outcome == "drift" and census_exit in (0, DRIFT_EXIT):
        return DRIFT_EXIT
    if census_exit == 0 and outcome == "host-unmatched":
        return HOST_UNDECLARED_EXIT
    if census_exit == 0 and outcome == "inventory-unobservable":
        return INVENTORY_UNOBSERVABLE_EXIT
    return census_exit


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise ValueError(f"unknown argument: {message}")


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments and arguments[0] == "schedule":
        from .runtime_lane_census_schedule import main as schedule_main

        return schedule_main(arguments[1:])
    if arguments and arguments[0] == "registry":
        from .runtime_lane_census_schedule import registry_main

        return registry_main(arguments[1:])
    parser = _Parser(description=__doc__)
    parser.add_argument(
        "--omni-home", dest="workspace_root", default=os.environ.get("OMNI_HOME", "")
    )
    parser.add_argument("--snapshot", default="")
    parser.add_argument("--memory", action="store_true")
    try:
        args = parser.parse_args(arguments)
    except ValueError as exc:
        sys.stderr.write(f"INDETERMINATE: {exc}\n")
        return 3
    if not args.workspace_root:
        sys.stderr.write(
            "INDETERMINATE: OMNI_HOME is not set and --omni-home was not passed.\n"
        )
        return 3
    try:
        schedule = load_schedule()
        root = Path(args.workspace_root)
        request = ModelLaneCensusCommand(
            collector=str(root / schedule.collector),
            snapshot=args.snapshot
            or str(root / schedule.state_directory / schedule.snapshot_name),
            memory=args.memory,
            timeout_s=schedule.timeout_seconds,
        )
    except (ValueError, ValidationError, OSError) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        return 3
    result = HandlerLaneCensusEffect().handle(request)
    for line in result.diagnostics:
        sys.stderr.write(f"[lane-census] {line}\n")
    sys.stdout.write(result.model_dump_json() + "\n")
    census_exit = result.exit_code
    if census_exit == 0 and not result.docker_observed:
        # A zero from a collector whose snapshot this run did not write observed nothing.
        sys.stderr.write("[lane-census] exit 0 but no fresh snapshot was written\n")
        census_exit = 4
    try:
        policy = (
            load_policy()
            if find_node_overlay(COMPOSE_CENSUS_NODE) is not None
            else None
        )
        check = _project_handler().handle(
            ModelComposeProjectCheckCommand(
                policy=policy,
                hostname=socket.gethostname(),
                home_dir=str(Path.home()),
                docker_binary=schedule.docker_binary,
                timeout_s=schedule.project_check_timeout_seconds,
            )
        )
    except (NodeOverlayError, ValidationError, ValueError, OSError) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        return 3
    for line in check.diagnostics:
        sys.stderr.write(f"[lane-census] project-check {line}\n")
    for finding in check.findings:
        sys.stderr.write(
            f"[lane-census] project-check {finding.container} {finding.kind.value} {finding.detail}\n"
        )
    sys.stdout.write(
        json.dumps({"compose_project_check": check.model_dump(mode="json")}) + "\n"
    )
    return _combined_exit(census_exit, check.outcome)


if __name__ == "__main__":
    raise SystemExit(main())
