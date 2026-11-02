# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the lane census in process on the host named by its local timer."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Never

from pydantic import ValidationError

from omnimarket.models.model_lane_census import ModelLaneCensusCommand

from .handlers.adapter_lane_census_schedule import load_schedule
from .handlers.handler_lane_census_effect import HandlerLaneCensusEffect


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise ValueError(f"unknown argument: {message}")


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments and arguments[0] == "schedule":
        from .runtime_lane_census_schedule import main as schedule_main

        return schedule_main(arguments[1:])
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
    if result.exit_code == 0 and not result.docker_observed:
        # A zero from a collector whose snapshot this run did not write observed nothing.
        sys.stderr.write("[lane-census] exit 0 but no fresh snapshot was written\n")
        return 4
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
