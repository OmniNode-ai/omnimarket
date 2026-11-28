# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Registry module tick main for the Claude pool refill read (OMN-24943).

    python -m omnimarket.nodes.node_claude_pool_refill_effect.runtime_claude_pool_refill_tick --omni-home <root> --state-dir <dir>

The host installer renders every ``registry_module_tick`` unit as that command. One pass runs the refill
handler over every host the pool overlay declares with ``dry_run=True`` (read and probe, add and mark nothing)
and writes the unit's ``last-run.json`` heartbeat. A pass that adds or marks a token needs the ``--live``
flag, which no declared unit passes. ``--probe`` exits 0 after one status line without loading an overlay,
reading the store or writing a file.
"""

from __future__ import annotations

import argparse
import json
import sys

from omnimarket.handlers.tick_unit_arguments import (
    add_tick_arguments,
    write_tick_heartbeat,
)
from omnimarket.nodes.node_claude_pool_refill_effect.handlers.handler_claude_pool_refill import (
    HandlerClaudePoolRefill,
)
from omnimarket.nodes.node_claude_pool_refill_effect.models.model_claude_pool_refill import (
    ModelClaudePoolRefillRequest,
)

PROBE_FLAG = "--probe"
PROBE_STATUS = "probe_ok"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="omnimarket.nodes.node_claude_pool_refill_effect.runtime_claude_pool_refill_tick",
        description="One read-only pass of the Claude pool refill over every host the pool overlay declares",
    )
    parser.add_argument(
        PROBE_FLAG,
        action="store_true",
        help="Start check: exit 0 and print one status line without loading an overlay, reading the store or writing a file",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="add and mark tokens too (default: read and probe only; no declared unit passes this)",
    )
    add_tick_arguments(parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    """--probe exits 0 after one status line; a pass writes last-run.json, the unit heartbeat."""
    args = _parser().parse_args(argv)
    if args.probe:
        sys.stdout.write(json.dumps({"status": PROBE_STATUS}, sort_keys=True) + "\n")
        return 0
    result = HandlerClaudePoolRefill().handle(
        ModelClaudePoolRefillRequest(dry_run=not args.live)
    )
    status = (
        "refused"
        if result.refusal
        else ("ok" if result.exit_code == 0 else "host_error")
    )
    line: dict[str, object] = {
        "status": status,
        "dry_run": not args.live,
        "lines": list(result.lines),
    }
    if result.refusal:
        line["refusal"] = result.refusal
    write_tick_heartbeat(args.state_dir, code=result.exit_code, line=line)
    for out in result.lines:
        sys.stdout.write(out + "\n")
    sys.stdout.write(json.dumps(line, sort_keys=True) + "\n")
    if result.refusal:
        sys.stderr.write(f"claude-pool-refill refused: {result.refusal}\n")
    return result.exit_code


__all__: list[str] = ["main"]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
