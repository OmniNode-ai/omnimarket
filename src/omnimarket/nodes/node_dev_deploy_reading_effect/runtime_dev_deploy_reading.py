# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_dev_deploy_reading_effect <verb>``.

``read``     gather the merges, receipts and probe and append the piece 11 reading row.
``probe``    the same reads, appending nothing.
``drill``    the same reads with the overlay's injected fault (an unreceipted merge), appended as a drill row.
``probe-merge`` read named dev merges (``--repo <name> --sha <commit>``) against the deploy agent and the running build, appending nothing.
``schedule`` check | install | uninstall | status | run-now, for a host in the overlay.

Each read and probe prints the events the run produced, one JSON object per line (topic and body).

Exit codes: 0 the run completed (a NOT WORKING reading is still 0: a reading refuses nothing);
1 the run failed (an append that did not land, a fact the model refused); 3 a deployment fact is
missing (no overlay, no host name, no append route).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError, find_node_overlay
from omnimarket.nodes.node_dev_deploy_reading_effect.adapters import LabDevDeployFacts
from omnimarket.nodes.node_dev_deploy_reading_effect.handlers.handler_dev_deploy_merge_probe import (
    HandlerDevDeployMergeProbe,
    ModelDevDeployMergeProbeCommand,
)
from omnimarket.nodes.node_dev_deploy_reading_effect.handlers.handler_dev_deploy_reading_run import (
    HandlerDevDeployReadingRun,
    events_of,
    load_topics,
)
from omnimarket.nodes.node_dev_deploy_reading_effect.handlers.schedule import (
    DevDeployReadingScheduler,
    load_schedule,
)
from omnimarket.nodes.node_dev_deploy_reading_effect.models.model_dev_deploy_reading_run import (
    ModelDevDeployReadingCommand,
)
from omnimarket.nodes.node_dev_deploy_reading_effect.overlay import (
    NODE_NAME,
    load_overlay,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
    build_appender,
)


def _run(arguments: argparse.Namespace, write: bool, drill: bool) -> int:
    overlay = load_overlay()
    host = os.environ.get(overlay.ledger_append.host_env, "").strip()
    if not host:
        raise ValueError(
            f"{overlay.ledger_append.host_env} is not set; the overlay names it as this host"
        )
    handler = HandlerDevDeployReadingRun(
        overlay=overlay,
        source=LabDevDeployFacts(overlay, os.environ),
        appender=build_appender(overlay.ledger_append, overlay.publish.lane)
        if write
        else None,
        clock=lambda: datetime.now(UTC),
        host=host,
        drill=drill,
    )
    result = handler.handle(
        ModelDevDeployReadingCommand(
            correlation_id=arguments.correlation_id or str(uuid.uuid4()), write=write
        )
    )
    for topic, body in events_of(result, load_topics()):
        sys.stdout.write(
            json.dumps({"topic": topic, "body": body}, sort_keys=True) + "\n"
        )
    sys.stdout.flush()
    return 0 if result.status == "completed" else 1


def _probe_merge(arguments: Sequence[str]) -> int:
    """Read named dev merges against the deploy agent and the running build; writes nothing.

    Exit 0 every merge read WORKING, UNKNOWN or NOT WORKING (a reading refuses nothing); 1 a merge could not be read
    (unknown repository, commit not on the ref); 3 the deploy agent or the build record could not be read.
    """
    parser = argparse.ArgumentParser(prog="probe-merge")
    parser.add_argument(
        "--repo", required=True, help="the merge repository's name in the overlay"
    )
    parser.add_argument(
        "--sha",
        required=True,
        action="append",
        help="a commit of the repository's dev ref",
    )
    args = parser.parse_args(list(arguments))
    overlay = load_overlay()
    handler = HandlerDevDeployMergeProbe(overlay, os.environ, lambda: datetime.now(UTC))
    code = 0
    for sha in args.sha:
        result = handler.handle(
            ModelDevDeployMergeProbeCommand(source_repo=args.repo, commit=sha)
        )
        sys.stdout.write(result.model_dump_json() + "\n")
        if result.failure is not None:
            code = max(code, 1)
        elif any(r.startswith("deploy-source-unread:") for r in result.reasons):
            code = 3
    sys.stdout.flush()
    return code


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
        if verb == "run-now":
            sub.add_argument("--drill", action="store_true", help="run the drill unit")
    args = parser.parse_args(list(arguments))
    scheduler = DevDeployReadingScheduler(
        schedule=load_schedule(), overlay=load_overlay(), home=Path.home()
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
        result = scheduler.run_now(args.host, drill=args.drill)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.exit_code


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if arguments and arguments[0] == "schedule":
            return _schedule(arguments[1:])
        if arguments and arguments[0] == "probe-merge":
            return _probe_merge(arguments[1:])
        parser = argparse.ArgumentParser(prog=NODE_NAME, description=__doc__)
        parser.add_argument("verb", choices=("read", "probe", "drill"))
        parser.add_argument("--correlation-id")
        args = parser.parse_args(arguments)
        return _run(args, write=args.verb != "probe", drill=args.verb == "drill")
    except (NodeOverlayError, LedgerRouteError, ValidationError, ValueError) as exc:
        sys.stderr.write(f"INDETERMINATE {type(exc).__name__}: {exc}\n")
        return 3


__all__ = ["main"]
