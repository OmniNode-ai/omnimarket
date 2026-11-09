# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One timer tick of the job-log filler, the unit the host installer renders (OMN-23032).

The unit passes ``--omni-home`` and ``--state-dir``. A tick runs one fill pass with the overlay's
default profile (the deployed gateway over the lane's bus, on the host that holds the PR watcher's
state) and writes its reading to ``<state-dir>/last-run.json``: when it ran, the pass status, the
counts, and ``coverage`` (filled plus already cached, over considered).

A tick takes an exclusive lock on ``<state-dir>/tick.lock`` before it reads or writes anything and holds it to the end,
so two ticks never share one state dir (an older install's tick left running beside the current unit's, OMN-23349). The
kernel drops the lock when its holder ends or dies. A tick that finds it held exits 3 and touches nothing.

Exit codes: 0 when the pass ended completed or partial (a log GitHub no longer serves fails on its own
and stays in ``failed``; it does not fail the tick), 1 when the pass failed (it could not start, or
every read failed), 2 when the overlay cannot be loaded. The evidence is written for all three, so a
tick that stopped filling shows in the file the floor reads.
"""

from __future__ import annotations

import argparse
import faulthandler
import fcntl
import json
import os
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_job_log_filler_effect.handlers.handler_job_log_filler import (
    HandlerJobLogFiller,
    load_overlay,
)
from omnimarket.nodes.node_job_log_filler_effect.models.model_job_log_filler import (
    EnumJobLogFillStatus,
    ModelJobLogFillRequest,
    ModelJobLogFillResult,
)

NODE = "node_job_log_filler_effect"
EVIDENCE_FILE = "last-run.json"
HELD_FILE = "held.json"
EXIT_FAILED = 1
EXIT_OVERLAY = 2
EXIT_LOCKED = 3
# The unit declares this name as tick_lock_file, so the installer can wait for, then stop, the tick that holds it.
LOCK_FILE = "tick.lock"


def tick_deadline_s() -> float:
    """The contract's bound on one tick; past it the process ends and the next tick starts clean."""
    contract = yaml.safe_load(
        (Path(__file__).parent / "contract.yaml").read_text(encoding="utf-8")
    )
    return float(contract["descriptor"]["timeout_ms"]) / 1000


def evidence_for(result: ModelJobLogFillResult, now: datetime) -> dict[str, Any]:
    """The tick's reading: the pass counts and the share of merge-relevant runs whose log is cached."""
    held = result.filled + result.already_cached
    return {
        "node": NODE,
        "ran_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": result.status.value,
        "profile": result.profile,
        "watcher_tick": result.watcher_tick,
        "considered": result.considered,
        "filled": result.filled,
        "already_cached": result.already_cached,
        "failed": result.failed,
        "deferred": result.deferred,
        "budget_exhausted": result.budget_exhausted,
        "stale_skipped": result.stale_skipped,
        "coverage": (held / result.considered) if result.considered else 1.0,
        "error": result.error,
    }


def read_held(path: Path) -> dict[int, float]:
    """The job ids the last passes confirmed cached, with when; an absent or unreadable file is an empty memo
    (every run is probed again), never a failed tick."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {int(job): float(at) for job, at in raw.items()}
    except (OSError, ValueError, AttributeError):
        return {}


def _write_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    held = path.with_suffix(path.suffix + ".tmp")
    held.write_text(text, encoding="utf-8")
    held.replace(path)


@contextmanager
def single_run(state_dir: Path) -> Iterator[bool]:
    """Hold the state dir's tick lock for the body; yields False, holding nothing, when another tick holds it."""
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / LOCK_FILE).open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()}\n")
        handle.flush()
        yield True


def main(
    argv: list[str] | None = None,
    *,
    handler: HandlerJobLogFiller | None = None,
    now: datetime | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog=f"{NODE}.tick", description=__doc__)
    parser.add_argument("--omni-home", dest="registry_root", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    with single_run(args.state_dir) as held:
        if not held:
            sys.stderr.write(
                f"{NODE}: another tick holds {args.state_dir / LOCK_FILE}; this one ran nothing\n"
            )
            return EXIT_LOCKED
        return run_pass(args, handler, now)


def run_pass(
    args: argparse.Namespace,
    handler: HandlerJobLogFiller | None,
    now: datetime | None,
) -> int:
    faulthandler.dump_traceback_later(tick_deadline_s(), exit=True)
    stamp = now or datetime.now(UTC)
    evidence_path = args.state_dir / EVIDENCE_FILE
    try:
        pass_handler = handler or HandlerJobLogFiller(
            overlay=load_overlay(), held=read_held(args.state_dir / HELD_FILE)
        )
    except (NodeOverlayError, ValueError, OSError) as exc:
        faulthandler.cancel_dump_traceback_later()
        reading = {
            "node": NODE,
            "ran_at": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": "overlay_unreadable",
            "error": f"{type(exc).__name__}: {exc}"[-2000:],
        }
        _write_atomically(evidence_path, json.dumps(reading, sort_keys=True) + "\n")
        sys.stderr.write(f"{NODE}: {reading['error']}\n")
        return EXIT_OVERLAY
    try:
        result = pass_handler.fill(ModelJobLogFillRequest(correlation_id=uuid.uuid4()))
    finally:
        faulthandler.cancel_dump_traceback_later()
    _write_atomically(
        args.state_dir / HELD_FILE,
        json.dumps({str(job): at for job, at in pass_handler.held.items()}) + "\n",
    )
    reading = evidence_for(result, stamp)
    _write_atomically(evidence_path, json.dumps(reading, sort_keys=True) + "\n")
    sys.stdout.write(json.dumps(reading, sort_keys=True) + "\n")
    return EXIT_FAILED if result.status is EnumJobLogFillStatus.FAILED else 0


__all__ = [
    "EVIDENCE_FILE",
    "EXIT_LOCKED",
    "HELD_FILE",
    "LOCK_FILE",
    "evidence_for",
    "main",
    "read_held",
    "single_run",
]

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
