#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CI coverage-sweep gate (OMN-14539) — GENERATE coverage.json, then sweep it.

Before this script, nothing in any repo ever generated ``coverage.json``.
``node_coverage_sweep`` (the compute handler) has always been a pure reader:
it expects a ``coverage.json`` artifact to already exist under each scanned
target dir and silently `continue`s past a missing one. With no producer
anywhere, that missing-artifact branch was the ONLY branch that ever fired in
production, and (pre-OMN-14539) it fell through to ``status="clean"`` — a
false green over zero measured modules (OMN-14531 audit).

This script is the harness the class fix requires: it is real, versioned,
CI-invoked code (never an operator-typed CLI flag, never SKILL.md prose) that

  1. GENERATES a real coverage.json for the target dir via
     ``coverage run -m pytest`` + ``coverage json`` (unless
     ``--skip-generate`` is passed, e.g. to sweep a pre-existing artifact in
     a test), then
  2. dispatches ``NodeCoverageSweep`` against that freshly generated
     artifact, and
  3. exits non-zero when the sweep's own safety invariant fails
     (``status == "error"``: coverage.json missing/unreadable, or zero
     modules were ever measured) — never when ``status == "gaps_found"``.

Coverage boundary (explicit, OMN-14531 Part 4): this gate covers the target
dir passed on the command line only (defaults to the current checkout, i.e.
omnimarket in CI). It does NOT run the fleet-wide 8-repo default scan
(``sweep_scope.DEFAULT_REPOS`` lives in sibling repos this job does not
check out) — that remains a follow-up (scheduled/cron dispatch), tracked
under OMN-14531. What this gate DOES guarantee is that the harness + handler
can no longer report a silent false-clean when it runs: any target dir
without a genuine coverage artifact is now a hard failure.

Exit codes:
  0 — coverage.json generated (or supplied) and swept; status is
      "clean" or "gaps_found" (existing coverage debt is informational,
      not a merge blocker — see aislop-sweep's new-vs-existing-debt
      precedent in ci.yml).
  1 — sweep reported status="error": no usable coverage census was ever
      produced. This is the invariant this gate exists to enforce.
  2 — coverage generation subprocess itself failed unexpectedly (engine
      error, not a coverage-content problem).
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

# Add src/ to path so omnimarket imports work in CI without editable install
_REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from omnimarket.nodes.node_coverage_sweep.handlers.handler_coverage_sweep import (  # noqa: E402
    CoverageSweepRequest,
    NodeCoverageSweep,
)


def _reap_process_group(proc: subprocess.Popen[bytes]) -> None:
    """Best-effort SIGTERM then SIGKILL of the child's ENTIRE process group.

    OMN-14645: coverage generation runs the full pytest suite, which can fan
    out into pytest-xdist workers and other spawned subprocesses. A plain
    ``proc.kill()`` on timeout reaps only the direct ``uv`` child and can
    orphan the rest, wedging or leaking the CI runner (a suspected
    runner-slot-hold vector behind the always-cancelled longest-pole job).
    Because the child is started in its own session (``start_new_session=True``)
    it is the leader of a fresh process group, so we can signal the whole group
    by its pgid. Purely best-effort — every lookup/signal is guarded because
    the group may already be gone.
    """
    if os.name != "posix":
        proc.kill()
        return
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, OSError):
            return
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue


def generate_coverage_json(
    target_dir: Path,
    *,
    test_path: str = "tests/",
    marker_expr: str = "not kafka",
    timeout_s: int = 1800,
    heartbeat_s: int = 60,
) -> tuple[bool, str]:
    """Run the real test suite under coverage and write ``coverage.json``.

    This is the harness's ONLY job with respect to the census: produce a
    genuine, freshly measured artifact. It intentionally does not catch and
    swallow subprocess failures — a failed generation run must not silently
    leave a stale or absent coverage.json behind and then let the sweep
    report clean over it.

    OMN-14645: the child is launched in its own session/process group
    (``start_new_session=True``) so that a timeout reaps the WHOLE pytest
    process tree, not just the direct ``uv`` child — orphaned coverage
    children are a runner-wedge vector on the longest-pole CI job. A timeout
    fails LOUDLY (``succeeded=False`` with an explicit message); it never
    leaves a stale artifact to be swept as clean.

    OMN-14645 / deconflict with OMN-14641 (#1776): the child's stdout/stderr
    are NOT captured — they STREAM straight through to this job's stdout/stderr.
    Capturing (``stdout=PIPE``/``stderr=PIPE``/``capture_output``) buffers the
    entire ~8-minute ``pytest --cov`` run until the process exits, which freezes
    the CI run's ``updatedAt`` for the whole longest-pole job. A frozen
    ``updatedAt`` is exactly what trips the Codex merge controller's stale-run
    heuristic into a cancel+rerun storm — the very failure this ticket exists to
    fix (controller self-diagnosed at the merge-controller ledger ~17:39Z). Do
    NOT re-add PIPE capture here: live streaming keeps the run heartbeating, and
    the timeout output above is preserved in the streamed job log, so there is no
    need to silently capture-and-hold it for a diagnostic tail.

    Returns ``(succeeded, message)``. ``succeeded=False`` does not itself
    fail the gate — the downstream sweep will observe the missing/invalid
    artifact and report ``status="error"``, which is what actually fails the
    gate (single source of truth for the invariant).
    """
    coverage_json = target_dir / "coverage.json"
    cmd = [
        "uv",
        "run",
        "pytest",
        test_path,
        "-m",
        marker_expr,
        "-q",
        f"--cov={target_dir / 'src'}",
        f"--cov-report=json:{coverage_json}",
    ]
    print(
        "coverage-sweep-gate: running coverage generation (streaming output): "
        + " ".join(cmd),
        flush=True,
    )
    try:
        # No stdout/stderr redirection: the child inherits this process's
        # stdout/stderr and streams live to the CI job log (see docstring —
        # capturing would freeze updatedAt and trigger the cancel storm).
        proc = subprocess.Popen(
            cmd,
            cwd=str(target_dir),
            start_new_session=True,
        )
    except OSError as exc:
        return False, f"coverage generation subprocess failed to start: {exc}"

    started_at = time.monotonic()
    next_heartbeat_at = started_at + heartbeat_s
    deadline_at = started_at + timeout_s

    while proc.poll() is None:
        now = time.monotonic()
        if now >= deadline_at:
            _reap_process_group(proc)
            return False, (
                f"coverage generation timed out after {timeout_s}s; process group "
                f"reaped (OMN-14645). The pytest output above (streamed live) shows "
                f"where it hung."
            )
        if now >= next_heartbeat_at:
            elapsed_s = int(now - started_at)
            print(
                "coverage-sweep-gate: coverage generation still running "
                f"after {elapsed_s}s",
                flush=True,
            )
            next_heartbeat_at = now + heartbeat_s
        time.sleep(min(1.0, max(0.1, next_heartbeat_at - now)))

    if proc.returncode:
        return False, (
            f"coverage generation exited {proc.returncode}. See the streamed "
            f"pytest output above for the failure."
        )

    if not coverage_json.is_file():
        return False, (
            f"coverage generation exited {proc.returncode} but produced no "
            f"coverage.json at {coverage_json}. See the streamed pytest output "
            f"above for the failure."
        )
    return True, f"generated {coverage_json}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target-dir",
        default=str(_REPO_ROOT),
        help="Directory to sweep (default: this repo's checkout root).",
    )
    parser.add_argument(
        "--target-pct",
        type=float,
        default=50.0,
        help="Coverage target percentage (default: 50).",
    )
    parser.add_argument(
        "--skip-generate",
        action="store_true",
        help=(
            "Sweep an already-present coverage.json instead of generating "
            "one. For local iteration only — CI always generates."
        ),
    )
    args = parser.parse_args(argv)

    target_dir = Path(args.target_dir).resolve()

    if not args.skip_generate:
        ok, message = generate_coverage_json(target_dir)
        print(f"coverage-sweep-gate: {message}")
        if not ok:
            # Generation failed outright (engine error) — do not silently
            # proceed to a sweep that will just report the artifact missing
            # for an unexplained reason; surface the engine failure directly.
            print(
                "coverage-sweep-gate: ERROR — coverage generation itself "
                "failed; see message above",
                file=sys.stderr,
            )
            return 2

    request = CoverageSweepRequest(
        target_dirs=[str(target_dir)],
        target_pct=args.target_pct,
    )
    result = NodeCoverageSweep().handle(request)
    print(json.dumps(result.model_dump(mode="json"), indent=2, default=str))

    if result.status == "error":
        print(
            "coverage-sweep-gate: status=error — "
            f"coverage_missing={result.coverage_missing!r}, "
            f"repos_scanned={result.repos_scanned}, "
            f"total_modules={result.total_modules}. Refusing to pass: an "
            "unmeasured scope is not a clean scope.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
