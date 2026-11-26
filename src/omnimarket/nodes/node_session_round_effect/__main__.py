# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Start one orchestrator round: ``python -m omnimarket.nodes.node_session_round_effect``.

Run by a round's launchd agent (node_host_installer_registry_units renders it with
``--omni-home <registry root> --state-dir <heartbeat directory>``). The unit's environment names
the round: ``ONEX_ROUND`` (its name), ``ONEX_ROUND_PROMPT`` (the prompt ``claude -p`` runs),
``ONEX_ROUND_CLAUDE`` (the claude binary), ``ONEX_ROUND_TIMEOUT_S`` and ``ONEX_ROUND_CLAUDE_ARGS``
(extra arguments, split like a shell line). Each option may be passed on the command line instead.
``--probe`` checks the inputs and runs nothing. Exit codes: the round's own, or 75 when the previous
run of the same round is alive, or 2 for a missing input.
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys
from pathlib import Path

from pydantic import ValidationError

from .handlers.handler_session_round import HandlerSessionRound
from .models.model_session_round import ModelSessionRoundRequest


def _parser() -> argparse.ArgumentParser:
    env = os.environ.get
    parser = argparse.ArgumentParser(prog="node_session_round_effect")
    parser.add_argument("--omni-home", type=Path, default=None)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--round", dest="round_name", default=env("ONEX_ROUND", ""))
    parser.add_argument("--prompt", default=env("ONEX_ROUND_PROMPT", ""))
    parser.add_argument("--claude", default=env("ONEX_ROUND_CLAUDE", "claude"))
    parser.add_argument("--timeout", default=env("ONEX_ROUND_TIMEOUT_S", ""))
    parser.add_argument(
        "--claude-args", default=env("ONEX_ROUND_CLAUDE_ARGS", ""), dest="claude_args"
    )
    parser.add_argument("--probe", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        request = ModelSessionRoundRequest(
            round_name=args.round_name,
            state_dir=args.state_dir,
            prompt=args.prompt,
            claude=args.claude,
            timeout_seconds=int(args.timeout) if args.timeout else 0,
            claude_arguments=tuple(shlex.split(args.claude_args)),
        )
    except (ValidationError, ValueError) as exc:
        sys.stderr.write(f"ROUND input refused: {exc}\n")
        return 2
    if args.probe:
        sys.stdout.write(
            f"ROUND {request.round_name} PROBE ok state_dir={request.state_dir}\n"
        )
        return 0
    result = HandlerSessionRound().handle(request)
    line = f"ROUND {result.round_name} {result.outcome.upper()} exit={result.exit_code}"
    if result.detail:
        line += f": {result.detail}"
    (sys.stderr if result.outcome == "busy" else sys.stdout).write(line + "\n")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
