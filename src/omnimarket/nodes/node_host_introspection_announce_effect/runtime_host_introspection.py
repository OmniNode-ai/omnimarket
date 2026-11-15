# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_host_introspection_announce_effect <verb>`` (OMN-19998).

``serve``  announce this host at start, on change and on the overlay's cadence until stopped (SIGINT or SIGTERM), then
           announce ``stop``.
``once``   publish one announcement and print it as JSON.

The host name is a deployment fact given on the command line, as for ``onex lab-work serve``. Every other value is the
``host_introspection`` overlay's. Exit codes: 0 done; 1 a reading or the bus failed; 3 a deployment fact is missing
(the overlay or the lab host table is not configured).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import signal
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from omnimarket.delegated_test_loop.lane_bus import BusKind, LabRunBusError
from omnimarket.handlers.host_introspection_overlay import (
    load_host_introspection_overlay,
)
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.overlay_keys import resolve_lab_hosts_table
from omnimarket.handlers.overlay_store.bus_opener import open_lane_bus, workspace_root
from omnimarket.nodes.node_host_introspection_announce_effect.adapters.local_host_facts_reader import (
    LocalHostFactsReader,
)
from omnimarket.nodes.node_host_introspection_announce_effect.handlers.handler_host_announcer import (
    HostAnnouncer,
)
from omnimarket.nodes.node_host_introspection_announce_effect.handlers.handler_host_introspection_gather import (
    HandlerHostIntrospectionGather,
)

EXIT_FAILED = 1
EXIT_INDETERMINATE = 3


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="node_host_introspection_announce_effect")
    parser.add_argument("verb", choices=("serve", "once"))
    parser.add_argument("--host-name", required=True)
    parser.add_argument("--bus", choices=("inmemory", "kafka"), default="kafka")
    parser.add_argument("--bus-lane", default=None)
    parser.add_argument("--kafka-bootstrap", default=None)
    parser.add_argument(
        "--workspace-root",
        type=Path,
        default=None,
        help="The checkout that holds the lane declarations (default: $OMNIBASE_PATH).",
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    overlay = load_host_introspection_overlay()
    gather = HandlerHostIntrospectionGather(
        overlay=overlay, reader=LocalHostFactsReader(), table=resolve_lab_hosts_table
    )
    async with open_lane_bus(
        bus=cast(BusKind, args.bus),
        lane=args.bus_lane,
        kafka_bootstrap=args.kafka_bootstrap,
        root=args.workspace_root or workspace_root(os.environ),
    ) as bus:
        announcer = HostAnnouncer(bus, gather, args.host_name, overlay.cadence_seconds)
        if args.verb == "once":
            published = await announcer.announce_once()
            if published is None:
                sys.stderr.write("nothing reached the bus\n")
                return EXIT_FAILED
            sys.stdout.write(published.model_dump_json(indent=2) + "\n")
            return 0 if announcer.failures == 0 else EXIT_FAILED
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        await announcer.run(stop)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except NodeOverlayError as exc:
        sys.stderr.write(f"INDETERMINATE: {exc}\n")
        return EXIT_INDETERMINATE
    except LabRunBusError as exc:
        sys.stderr.write(f"bus: {exc}\n")
        return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
