# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Registry module tick main for the Claude token pool rotation tick (OMN-23000).

    python -m omnimarket.nodes.node_claude_token_pool_effect.runtime_claude_pool_tick --omni-home <root> --state-dir <dir>

The host installer renders every ``registry_module_tick`` unit as that command, so this main accepts the
template's ``--omni-home`` and ``--state-dir`` (via :func:`add_tick_arguments`) and writes the unit's
``last-run.json`` heartbeat after the pass. ``--probe`` exits 0 after one status line without loading an
overlay, reading the bus or writing a file.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.tick_unit_arguments import (
    add_tick_arguments,
    write_tick_heartbeat,
)
from omnimarket.nodes.node_claude_token_pool_effect.runtime_claude_token_pool import (
    EXIT_ALL_LIMITED,
    EXIT_EMPTY,
    run_tick,
)

HOST_ENV = "ONEX_HOST_NAME"
PROBE_FLAG = "--probe"
PROBE_STATUS = "probe_ok"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="omnimarket.nodes.node_claude_token_pool_effect.runtime_claude_pool_tick",
        description="One pass of the Claude token pool rotation tick",
    )
    parser.add_argument(
        PROBE_FLAG,
        action="store_true",
        help="Start check: exit 0 and print one status line without loading an overlay, reading the bus or writing a file",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="decide and write nothing"
    )
    parser.add_argument("--host", default=None, help="this host's name in the overlay")
    add_tick_arguments(parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    """--probe exits 0 after one status line; a pass accepts --omni-home and --state-dir and writes last-run.json, the unit heartbeat."""
    parser = _parser()
    args = parser.parse_args(argv)
    if args.probe:
        sys.stdout.write(json.dumps({"status": PROBE_STATUS}, sort_keys=True) + "\n")
        return 0
    host = args.host or os.environ.get(HOST_ENV)
    if not host:
        line: dict[str, object] = {
            "status": "host_missing",
            "detail": f"{HOST_ENV} is not set and --host was not passed",
        }
        code = EXIT_EMPTY
        write_tick_heartbeat(args.state_dir, code=code, line=line)
        sys.stdout.write(json.dumps(line, sort_keys=True) + "\n")
        return code
    try:
        result = run_tick(host, dry_run=args.dry_run)
    except (NodeOverlayError, ValidationError, ValueError, OSError) as exc:
        line = {"status": "overlay_refused", "detail": str(exc)}
        code = EXIT_EMPTY
    else:
        line = {
            "status": result.outcome,
            "host": result.host,
            "rank": result.rank,
            "marks": result.marks_sync.state,
            "stale_revoke": result.stale_revoke,
            "revokes_cleared": list(result.revokes_cleared),
            "revokes_kept": list(result.revokes_kept),
        }
        if result.outcome == "no_usable":
            code = EXIT_ALL_LIMITED
        elif result.marks_sync.state in ("unreadable", "stale"):
            code = EXIT_EMPTY
        else:
            code = 0
    write_tick_heartbeat(args.state_dir, code=code, line=line)
    sys.stdout.write(json.dumps(line, sort_keys=True) + "\n")
    return code


__all__: list[str] = ["main"]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
