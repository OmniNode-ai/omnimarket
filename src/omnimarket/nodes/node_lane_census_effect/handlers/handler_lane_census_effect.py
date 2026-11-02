# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the lane census once and prove the run from what it left behind.

The collector's exit code is a verdict (``0`` clean, ``30`` drift, ``4`` the Docker inventory
could not be observed), but it does not say the snapshot on disk is this run's. A run therefore
counts as having observed the inventory only when the collector reached a verdict *and* the
snapshot file was written at or after the moment the run started: a stale snapshot from an
earlier tick, or from a host that stopped being able to read Docker, proves nothing.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from omnimarket.models.model_lane_census import (
    OUTCOME_BY_EXIT,
    ModelLaneCensusCommand,
    ModelLaneCensusRunResult,
)

Runner = Callable[[Sequence[str], int], "subprocess.CompletedProcess[str]"]
Clock = Callable[[], float]

# Exit codes on which the collector did not read the inventory.
_NO_INVENTORY = frozenset({2, 3, 4, 5})


def run_collector(
    argv: Sequence[str], timeout_s: int
) -> subprocess.CompletedProcess[str]:
    """Run the collector; a missing shell or a timeout reads as a non-verdict exit code."""
    try:
        return subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_s,
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(list(argv), 127, "", str(exc))
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            list(argv), 124, "", f"collector exceeded {timeout_s}s"
        )


class HandlerLaneCensusEffect:
    """handle(ModelLaneCensusCommand) -> ModelLaneCensusRunResult."""

    def __init__(
        self, *, run: Runner = run_collector, clock: Clock = time.time
    ) -> None:
        self._run = run
        self._clock = clock

    def handle(self, request: ModelLaneCensusCommand) -> ModelLaneCensusRunResult:
        snapshot = Path(request.snapshot)
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        argv = ["bash", request.collector, "--snapshot", request.snapshot]
        if request.memory:
            argv.append("--memory")
        started = self._clock()
        done = self._run(argv, request.timeout_s)
        outcome = OUTCOME_BY_EXIT.get(done.returncode, "collector-error")
        fresh = snapshot.is_file() and snapshot.stat().st_mtime >= started
        observed = fresh and done.returncode not in _NO_INVENTORY
        diagnostics = tuple(
            line for line in (done.stderr or "").splitlines() if line.strip()
        )
        return ModelLaneCensusRunResult(
            exit_code=done.returncode,
            outcome=outcome,
            snapshot_fresh=fresh,
            docker_observed=observed,
            snapshot=request.snapshot,
            diagnostics=diagnostics,
        )
