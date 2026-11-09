# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One tick of a host's lane closeout sweep, the unit the host installer renders (OMN-23375).

The unit passes ``--omni-home`` and ``--state-dir``; every other setting is the node's overlay. ``--read``
changes nothing and prints the reading. ``--lane`` closes out one lane. The exit code is 0 when no ended
lane is left without a closeout record, 3 when one is, 2 when the overlay cannot be read.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from pathlib import Path

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.models.lane_closeout.model_lane_closeout_sweep_request import (
    ModelLaneCloseoutSweepRequest,
)
from omnimarket.models.lane_closeout.model_lane_closeout_sweep_settings import (
    ModelLaneCloseoutSweepSettings,
)
from omnimarket.nodes.node_lane_closeout_sweep_effect.handlers.handler_lane_closeout_sweep import (
    HandlerLaneCloseoutSweep,
)

NODE = "node_lane_closeout_sweep_effect"
EXIT_OK = 0
EXIT_UNREADABLE = 2
EXIT_UNRECORDED = 3
HOST_ENV = "LAB_WORK_HOST_NAME"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=f"{NODE}.tick", description=__doc__)
    parser.add_argument("--omni-home", dest="registry_root", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--lane", default=None)
    parser.add_argument("--read", action="store_true")
    args = parser.parse_args(argv)
    env = os.environ
    try:
        settings = load_structured_overlay(NODE, ModelLaneCloseoutSweepSettings, env)
    except NodeOverlayError as exc:
        sys.stderr.write(f"{NODE}: {exc}\n")
        return EXIT_UNREADABLE
    host = env.get(HOST_ENV, "").strip() or socket.gethostname()
    result = HandlerLaneCloseoutSweep().handle(
        ModelLaneCloseoutSweepRequest(
            registry_root=str(args.registry_root.resolve()),
            state_dir=str(args.state_dir.resolve()),
            host=host,
            settings=settings,
            now=time.time(),
            lane=args.lane,
            read_only=args.read,
        )
    )
    reading = {
        "host": result.host,
        "ended_lanes": result.ended_lanes,
        "unrecorded": result.unrecorded,
        "unrecorded_lanes": list(result.unrecorded_lanes),
        "closed": len(result.closed),
        "closed_complete": sum(1 for r in result.closed if r.complete),
        "closed_kept": sum(1 for r in result.closed if not r.complete),
        "skipped_live": list(result.skipped_live),
        "read_only": result.read_only,
    }
    line = json.dumps(reading, sort_keys=True)
    sys.stdout.write(line + "\n")
    if not args.read:
        evidence = args.state_dir / "last-run.json"
        args.state_dir.mkdir(parents=True, exist_ok=True)
        evidence.write_text(line + "\n", encoding="utf-8")
    return EXIT_UNRECORDED if result.unrecorded else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
