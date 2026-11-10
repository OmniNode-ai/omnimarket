# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_runner_group_exposure_effect <verb>``.

``hourly``    read the runner group and append one reading row (what the schedule runs).
``probe``     the same read, appending nothing.
``schedule``  check | install | uninstall | status | run-now, for a host in the overlay.

Exit codes: 0 the group was read and its row written (a NOT_WORKING reading is still 0: a reading
changes nothing and refuses nothing); 1 the group was not read, or the append did not land, or a
host the overlay does not declare; 3 a deployment fact is missing (INDETERMINATE).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError, find_node_overlay
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
    build_appender,
)
from omnimarket.nodes.node_runner_group_exposure_effect.handlers.handler_exposure_probe import (
    HandlerExposureProbe,
)
from omnimarket.nodes.node_runner_group_exposure_effect.handlers.handler_exposure_reading_writer import (
    HandlerExposureReadingWriter,
)
from omnimarket.nodes.node_runner_group_exposure_effect.handlers.schedule import (
    ExposureScheduler,
    load_schedule,
)
from omnimarket.nodes.node_runner_group_exposure_effect.models.model_exposure_reading import (
    ModelExposureReadingCommand,
)
from omnimarket.nodes.node_runner_group_exposure_effect.overlay import (
    NODE_NAME,
    load_reading_overlay,
)


def _run(write: bool) -> int:
    overlay = load_reading_overlay()
    host = os.environ.get(overlay.ledger_append.host_env, "").strip()
    if not host:
        raise ValueError(
            f"{overlay.ledger_append.host_env} is not set; the overlay names it as this host"
        )
    now = datetime.now(UTC)
    handler = HandlerExposureReadingWriter(
        overlay=overlay,
        host=host,
        probe=HandlerExposureProbe(overlay=overlay),
        appender=build_appender(overlay.ledger_append, overlay.publish.lane)
        if write
        else None,
    )
    outcome = handler.handle(
        ModelExposureReadingCommand(
            correlation_id=f"runner-group-exposure-{host}-{now:%Y-%m-%dT%H:%M:%SZ}",
            now=now,
            write=write,
        )
    )
    sys.stdout.write(
        f"READING host={outcome.host} backend={outcome.backend} verdict={outcome.verdict.value} "
        f"outcome={outcome.outcome.value} ledger={outcome.ledger} key={outcome.row_key} "
        f"calls={outcome.calls} reason={','.join(outcome.reasons)}\n"
    )
    return 1 if outcome.failed else 0


def _schedule(arguments: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="schedule")
    verbs = parser.add_subparsers(dest="verb", required=True)
    verbs.add_parser("check")
    for verb in ("install", "uninstall", "status", "run-now"):
        sub = verbs.add_parser(verb)
        sub.add_argument("--host", required=True, help="the host's name in the overlay")
        if verb == "install":
            sub.add_argument(
                "--workspace-root",
                dest="workspace_root",
                default=os.environ.get("OMNI_HOME", ""),
            )
            sub.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(list(arguments))
    scheduler = ExposureScheduler(
        schedule=load_schedule(), overlay=load_reading_overlay(), home=Path.home()
    )
    if args.verb == "check":
        result = scheduler.check()
    elif args.verb == "install":
        if not args.workspace_root:
            raise ValueError("OMNI_HOME is not set and --workspace-root was not passed")
        found = find_node_overlay(NODE_NAME)
        if found is None:
            raise NodeOverlayError(
                "the overlay file cannot be found to pin its root in the unit"
            )
        result = scheduler.install(
            args.host,
            omni_root=Path(args.workspace_root),
            overlay_root=found.parent.parent,
            dry_run=args.dry_run,
        )
    elif args.verb == "uninstall":
        result = scheduler.uninstall(args.host)
    elif args.verb == "status":
        result = scheduler.status(args.host)
    else:
        result = scheduler.run_now(args.host)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.exit_code


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if arguments and arguments[0] == "schedule":
            return _schedule(arguments[1:])
        parser = argparse.ArgumentParser(prog=NODE_NAME, description=__doc__)
        parser.add_argument("verb", choices=("hourly", "probe"))
        args = parser.parse_args(arguments)
        return _run(write=args.verb == "hourly")
    except (
        NodeOverlayError,
        LedgerRouteError,
        ValidationError,
        ValueError,
        OSError,
    ) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
