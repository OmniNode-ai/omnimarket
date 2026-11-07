# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Entry: ``onex run-node node_floor_fill_orchestrator -- <command> [key]``.

The entry holds no I/O of its own: the handler it builds takes every seam in its constructor. The
shipped profile is ``inmemory`` (scripted seams over an in-memory conditional store); the live bindings of
the facts, pool, ledger, tickets and process seams are bound by the lab step (plan T8) and refuse here, naming
what is unbound. The entry ends the process itself at ``floor_fill.command_timeout_seconds``, so a stalled
command leaves no process behind.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from omnimarket.nodes.node_floor_fill_orchestrator.handlers.handler_floor_fill_command import (
    HandlerFloorFillCommand,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models import (
    EXIT_CODE_BY_STATUS,
    EnumFloorFillCommand,
    EnumFloorFillStatus,
    FloorFillConfigError,
    ModelFloorFillCommandRequest,
    ModelFloorFillConfig,
    ModelFloorFillEntryResult,
    load_floor_fill_config,
)

OVERLAY_SECTION = "floor_fill"


def run_entry(
    argv: Sequence[str],
    overlay: Mapping[str, Any],
    *,
    host_id: str,
    build_handler: Callable[[ModelFloorFillConfig], HandlerFloorFillCommand],
) -> ModelFloorFillEntryResult:
    """Parse the command, build the handler from the overlay and run it. A refused overlay prints and fails."""
    parser = argparse.ArgumentParser(prog="node_floor_fill_orchestrator")
    parser.add_argument("command", choices=[c.value for c in EnumFloorFillCommand])
    parser.add_argument("key", nargs="?", default=None)
    args = parser.parse_args(list(argv))
    try:
        config = load_floor_fill_config(overlay.get(OVERLAY_SECTION, {}))
    except FloorFillConfigError as error:
        return ModelFloorFillEntryResult(
            exit_code=EXIT_CODE_BY_STATUS[EnumFloorFillStatus.FAILED],
            stdout=f"UNKNOWN fill-state:overlay:{','.join(error.keys) or error}\n",
        )
    return build_handler(config).handle(
        ModelFloorFillCommandRequest(
            command=EnumFloorFillCommand(args.command), key=args.key, host_id=host_id
        )
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--overlay-json", required=True)
    parser.add_argument("--this-host", required=True)
    parser.add_argument("--profile", default="inmemory")
    known, rest = parser.parse_known_args(sys.argv[1:] if argv is None else list(argv))
    if known.profile != "inmemory":
        sys.stdout.write(
            f"UNKNOWN fill-state:profile-unbound:{known.profile} (live seams are bound by the lab step)\n"
        )
        return EXIT_CODE_BY_STATUS[EnumFloorFillStatus.FAILED]
    overlay = json.loads(known.overlay_json)
    from omnimarket.nodes.node_floor_fill_orchestrator.container.container_inmemory import (
        build_inmemory_handler,
    )

    result = run_entry(
        rest, overlay, host_id=known.this_host, build_handler=build_inmemory_handler
    )
    sys.stdout.write(result.stdout)
    sys.stdout.flush()
    if result.timed_out:
        # The command is still running in a worker thread: end the process, so none remains.
        os._exit(result.exit_code)
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
