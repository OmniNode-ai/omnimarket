# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One tick of the review key credit probe, the unit the host installer renders (OMN-24656).

The unit passes ``--omni-home`` and ``--state-dir``. The tick reads the probe's own overlay
(``<root>/review_key_credit_probe/overlay.yaml``: tenant, model, host row), runs ``onex models
review-gateway-key-probe`` once beside the running interpreter, and writes ``last-run.json`` into the state
directory: exit 0 when the probe ran, 1 when it failed, 124 when it ran past its bound, 3 when the overlay is
missing or invalid (nothing is probed), and 2 when the run carries no ``--state-dir`` (nothing is probed).
The probe's own stdout and stderr pass through to the tick's streams, so a failing probe's reason lands in the
unit's logs. The probe command takes the destination of a credit flip from the unit's environment.
``--probe`` checks the module without a pass, a bus read or a write.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

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
# The probe's own overlay id: the gateway's overlay (event_bus, contract) is a different document.
OVERLAY_ID = "review_key_credit_probe"
# Under the unit's run_timeout_s, so a hung probe ends here with its own record and not at the unit's wrapper.
PROBE_TIMEOUT_SECONDS = 540
REFUSED = 2
INDETERMINATE = 3
TIMED_OUT = 124
FLIP_COUNT_LINE = re.compile(r"^review-key-probe: \d+ flip\(s\) written$")

Runner = Callable[[list[str], int], subprocess.CompletedProcess[str]]


def run_command(argv: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, check=False
    )


def onex_executable() -> str:
    """The ``onex`` console script installed beside the running interpreter, never a PATH lookup."""
    return str(Path(sys.executable).with_name("onex"))


def probe_argv(overlay: ModelReviewKeyProbeOverlay, state_dir: Path) -> list[str]:
    argv = [
        onex_executable(),
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


def flip_count_line(stdout: str) -> str:
    """The probe's flip-count line, or nothing: a key's name and state are not a summary."""
    lines = stdout.strip().splitlines()
    last = lines[-1] if lines else ""
    return last if FLIP_COUNT_LINE.match(last) else ""


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
    state_dir: Path | None = args.state_dir
    if state_dir is None:
        sys.stderr.write(
            "REFUSED: --state-dir is required for a pass; nothing was probed\n"
        )
        return REFUSED
    try:
        overlay = load_structured_overlay(OVERLAY_ID, ModelReviewKeyProbeOverlay)
    except NodeOverlayError as exc:
        sys.stderr.write(f"INDETERMINATE: {exc}\n")
        write_tick_heartbeat(
            state_dir,
            code=INDETERMINATE,
            line={"indeterminate": type(exc).__name__},
        )
        return INDETERMINATE
    try:
        done = run(probe_argv(overlay, state_dir), PROBE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        sys.stderr.write(f"TIMED OUT: the probe ran past {PROBE_TIMEOUT_SECONDS} s\n")
        write_tick_heartbeat(
            state_dir,
            code=TIMED_OUT,
            line={"timed_out_after_s": PROBE_TIMEOUT_SECONDS},
        )
        return TIMED_OUT
    except OSError as exc:
        sys.stderr.write(f"PROBE DID NOT RUN: {type(exc).__name__}: {exc}\n")
        write_tick_heartbeat(
            state_dir, code=1, line={"probe_did_not_run": type(exc).__name__}
        )
        return 1
    sys.stdout.write(done.stdout)
    sys.stderr.write(done.stderr)
    code = 0 if done.returncode == 0 else 1
    write_tick_heartbeat(
        state_dir,
        code=code,
        line={"probe_exit": done.returncode, "summary": flip_count_line(done.stdout)},
    )
    return code


if __name__ == "__main__":
    sys.exit(main())
