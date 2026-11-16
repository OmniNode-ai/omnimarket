# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One tick of the review key credit probe, the unit the host installer renders (OMN-24656).

The unit passes ``--omni-home`` and ``--state-dir``. The tick reads the node's overlay (tenant, model, host row),
runs ``onex models review-gateway-key-probe`` once, and writes ``last-run.json`` into the state directory: exit 0
when the probe ran, 1 when it failed, 124 when it ran past its bound, and 3 when the overlay is missing or
invalid, in which case nothing is probed. The probe command takes the destination of a credit flip from the
unit's environment. ``--probe`` checks the module without a pass, a bus read or a write.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.handlers.tick_unit_arguments import (
    add_tick_arguments,
    write_tick_heartbeat,
)

from .models.model_review_key_probe_overlay import ModelReviewKeyProbeOverlay

NODE_NAME = "node_claude_review_gateway_effect"
# Under the unit's run_timeout_s, so a hung probe ends here with its own record and not at the unit's wrapper.
PROBE_TIMEOUT_SECONDS = 540
INDETERMINATE = 3
TIMED_OUT = 124

Runner = Callable[[list[str], int], subprocess.CompletedProcess[str]]


def run_command(argv: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, check=False
    )


def probe_argv(overlay: ModelReviewKeyProbeOverlay, state_dir: Path) -> list[str]:
    argv = [
        "onex",
        "models",
        "review-gateway-key-probe",
        "--tenant",
        overlay.tenant_id,
        "--model",
        overlay.model,
    ]
    if overlay.host is not None:
        argv += ["--host", overlay.host]
    return [*argv, "--state-dir", str(state_dir / "keys")]


def main(argv: list[str] | None = None, run: Runner = run_command) -> int:
    parser = argparse.ArgumentParser(
        prog=f"{NODE_NAME}.probe_tick", description=__doc__
    )
    parser.add_argument(
        "--probe", action="store_true", help="check the module and exit; probes nothing"
    )
    add_tick_arguments(parser)
    args = parser.parse_args(argv)
    if args.probe:
        return 0
    try:
        overlay = load_structured_overlay(NODE_NAME, ModelReviewKeyProbeOverlay)
    except (NodeOverlayError, ValueError, ValidationError) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}\n")
        write_tick_heartbeat(
            args.state_dir,
            code=INDETERMINATE,
            line={"indeterminate": type(exc).__name__},
        )
        return INDETERMINATE
    state_dir = args.state_dir if args.state_dir is not None else Path(".")
    try:
        done = run(probe_argv(overlay, state_dir), PROBE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        write_tick_heartbeat(
            args.state_dir,
            code=TIMED_OUT,
            line={"timed_out_after_s": PROBE_TIMEOUT_SECONDS},
        )
        return TIMED_OUT
    except OSError as exc:
        write_tick_heartbeat(
            args.state_dir, code=1, line={"probe_did_not_run": type(exc).__name__}
        )
        return 1
    last = done.stdout.strip().splitlines()[-1] if done.stdout.strip() else ""
    code = 0 if done.returncode == 0 else 1
    write_tick_heartbeat(
        args.state_dir, code=code, line={"probe_exit": done.returncode, "summary": last}
    )
    return code


if __name__ == "__main__":
    sys.exit(main())
