# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane's leader process: ``onex run-node node_remote_lane_start_orchestrator -- <args>`` (OMN-22848).

It stays alive for the lane's life. It places the lane, appends its CLAIM itself, starts the
engine detached in a session of its own on the placed host, supervises it and appends the
TERMINAL (or a STATUS when the lane closed itself). A SIGTERM stops the engine and closes the
lane only once the stop is confirmed. The exit code is diagnostic only; the ledger decides.

    python -m omnimarket.nodes.node_remote_lane_start_orchestrator.runtime_remote_lane_start --overlay <overlay.yaml> \\
        --ledger <ledger.md> --pool-snapshot <pool.json> --lane <lane> --key <key> \\
        --ticket OMN-<n> --model <model> --brief <brief.md> --run-id <run> [--attempt N] \\
        [--engine claude] [--placement lab] [--parent <lane>] [--repo R --owner O --sha S] \\
        [--repair-target <host> --ssh-reach-file <reach.json>]

There is no default ledger and no environment read: a missing ``--ledger`` refuses, and every
setting is a ``remote_lane_start.*`` key of the overlay (a missing key refuses naming it).
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path

import yaml
from omnibase_spi.contracts.services import ModelLanePluginTree

from omnimarket.nodes.node_remote_lane_start_orchestrator.handlers.adapter_host_pool import (
    SnapshotFileLaneHostPool,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.handlers.handler_remote_lane_start import (
    HandlerRemoteLaneStart,
    build_real,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.models.model_remote_lane_start import (
    ModelRemoteLaneStartRequest,
    ModelRemoteLaneStartSettings,
)

EXIT_USAGE = 64


def _say(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="node_remote_lane_start_orchestrator")
    p.add_argument("--overlay", required=True)
    p.add_argument(
        "--ledger",
        required=True,
        help="the rolling ledger the CLAIM and TERMINAL go to (no default)",
    )
    p.add_argument("--pool-snapshot", required=True)
    p.add_argument("--lane", required=True)
    p.add_argument("--key", help="the fill's key; no key writes no key= cell")
    p.add_argument("--ticket", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--brief", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--attempt", type=int, default=1)
    p.add_argument("--engine", default="claude")
    p.add_argument("--placement", default="lab")
    p.add_argument("--parent")
    p.add_argument("--pinned-host")
    p.add_argument(
        "--repair-target",
        help="the host this lane repairs: the lane runs on another host that reached it over ssh (OMN-23389)",
    )
    p.add_argument(
        "--ssh-reach-file",
        help="JSON {host: [hosts it reached over ssh]} read beside the pool snapshot; a host absent from it takes no repair lane",
    )
    p.add_argument("--escalation-reason")
    p.add_argument("--repo")
    p.add_argument("--owner")
    p.add_argument("--sha")
    p.add_argument(
        "--plugin-tree",
        action="append",
        default=[],
        metavar="NAME:DIGEST:ARCHIVE",
        help="a plugin tree to ship to the host: its name, the digest naming the installed copy and its archive on this host; repeat per tree",
    )
    p.add_argument(
        "--timeout-min",
        type=int,
        help="the lane's runtime limit; the CLAIM's est hours follow it",
    )
    return p


def plugin_tree(spec: str) -> ModelLanePluginTree:
    """One ``--plugin-tree NAME:DIGEST:ARCHIVE`` value; the archive path may itself hold colons."""
    name, digest, archive = [*spec.split(":", 2), "", ""][:3]
    return ModelLanePluginTree(name=name, source_digest=digest, archive_path=archive)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        overlay = yaml.safe_load(Path(args.overlay).read_text(encoding="utf-8")) or {}
        settings = ModelRemoteLaneStartSettings.from_overlay(overlay)
        request = ModelRemoteLaneStartRequest(
            lane=args.lane,
            key=args.key,
            ticket=args.ticket,
            parent=args.parent,
            engine=args.engine,
            placement=args.placement,
            model=args.model,
            ledger_path=args.ledger,
            brief=Path(args.brief).read_text(encoding="utf-8"),
            run_id=args.run_id,
            attempt=args.attempt,
            pinned_host=args.pinned_host,
            repair_target=args.repair_target,
            host_ssh_reach=(
                json.loads(Path(args.ssh_reach_file).read_text(encoding="utf-8"))
                if args.ssh_reach_file
                else {}
            ),
            escalation_reason=args.escalation_reason,
            repo=args.repo,
            owner=args.owner,
            sha=args.sha,
            timeout_min=args.timeout_min,
            plugin_trees=tuple(plugin_tree(spec) for spec in args.plugin_tree),
        )
    except (OSError, KeyError, ValueError) as exc:
        sys.stderr.write(f"node_remote_lane_start_orchestrator: refused: {exc}\n")
        return EXIT_USAGE
    stop = {"requested": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__("requested", True))
    handler: HandlerRemoteLaneStart = build_real(
        settings,
        SnapshotFileLaneHostPool(Path(args.pool_snapshot)),
        stop_requested=lambda: stop["requested"],
        sleep=time.sleep,
        echo=_say,
    )
    result = handler.handle(request)
    _say(
        f"REMOTE-LANE-RESULT lane={request.lane} outcome={result.outcome.value} exit={result.exit_code}"
    )
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
