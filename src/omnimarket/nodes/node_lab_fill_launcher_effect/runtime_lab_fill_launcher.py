# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for the lab-fill launcher effect.

Parses the tick's arguments, loads the overlay, wires the local adapters, runs
the handler, and prints one JSON line (the verdict) to stdout. A fire whose
overlay cannot be loaded or whose state directory is missing is refused with a
single ``lab-fill-launcher: REFUSED: <reason>`` line on stderr and exit 78.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_lab_fill_launcher_effect.adapters.local_lab_fill_launcher import (
    LocalAlertSender,
    LocalFireFiles,
    SystemClock,
)
from omnimarket.nodes.node_lab_fill_launcher_effect.handlers.handler_lab_fill_launcher import (
    HandlerLabFillLauncher,
)
from omnimarket.nodes.node_lab_fill_launcher_effect.models.model_lab_fill_launcher import (
    ModelLabFillLauncherRequest,
)
from omnimarket.nodes.node_lab_fill_launcher_effect.overlay import (
    NODE_NAME,
    load_overlay,
)


def main(
    argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None
) -> int:
    """Judge one fire: load the overlay, run the handler, print the verdict line."""
    parser = argparse.ArgumentParser(prog=NODE_NAME, description=__doc__)
    parser.add_argument("--fire-id", required=True, help="The fire's id.")
    parser.add_argument(
        "--phase", required=True, help="The phase the run left the fire in."
    )
    parser.add_argument(
        "--exit",
        required=True,
        type=int,
        help="The exit code the run left the fire with.",
    )
    parser.add_argument("--run-log", required=True, help="Path to the run log.")
    parser.add_argument(
        "--workflow-result-file",
        default="",
        help='The path named in the truncated notice ("" when none).',
    )
    parser.add_argument(
        "--selftest", action="store_true", help="A selftest fire is returned unjudged."
    )
    parser.add_argument(
        "--result-channel",
        action="store_true",
        help="The fire had a workflow-result file named.",
    )
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    env = dict(os.environ if environ is None else environ)

    def refuse(reason: str) -> int:
        sys.stderr.write(f"lab-fill-launcher: REFUSED: {reason}\n")
        return 78

    try:
        overlay = load_overlay(env)
    except NodeOverlayError as exc:
        return refuse(f"the overlay could not be loaded: {exc}")

    home = env.get(overlay.state.home_env, "").strip()
    if not home:
        return refuse(
            f"{overlay.state.home_env} is not set; the fire's files live under it"
        )
    state_dir = Path(home) / overlay.state.state_relpath
    if not state_dir.is_dir():
        return refuse(f"the state directory is missing: {state_dir}")

    handler = HandlerLabFillLauncher(
        overlay=overlay,
        files=LocalFireFiles(Path(home), overlay.state),
        alerts=LocalAlertSender(overlay.alert, overlay.state, env),
        clock=SystemClock(),
    )
    result = handler.handle(
        ModelLabFillLauncherRequest(
            fire_id=args.fire_id,
            phase=args.phase,
            exit=args.exit,
            selftest=args.selftest,
            workflow_result_file=args.workflow_result_file,
            run_log=args.run_log,
            result_channel_enabled=args.result_channel,
        )
    )
    sys.stdout.write(result.tick_line() + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
