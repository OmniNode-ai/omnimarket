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
import subprocess
import sys
from pathlib import Path

# Add src/ to path so omnimarket imports work in CI without editable install
_REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from omnimarket.nodes.node_coverage_sweep.handlers.handler_coverage_sweep import (  # noqa: E402
    CoverageSweepRequest,
    NodeCoverageSweep,
)


def generate_coverage_json(
    target_dir: Path,
    *,
    test_path: str = "tests/",
    marker_expr: str = "not kafka",
    timeout_s: int = 1800,
) -> tuple[bool, str]:
    """Run the real test suite under coverage and write ``coverage.json``.

    This is the harness's ONLY job with respect to the census: produce a
    genuine, freshly measured artifact. It intentionally does not catch and
    swallow subprocess failures — a failed generation run must not silently
    leave a stale or absent coverage.json behind and then let the sweep
    report clean over it.

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
        "coverage-sweep-gate: running coverage generation command: " + " ".join(cmd),
        flush=True,
    )
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(target_dir),
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return False, f"coverage generation timed out after {timeout_s}s: {exc}"
    except OSError as exc:
        return False, f"coverage generation subprocess failed to start: {exc}"

    if not coverage_json.is_file():
        return False, (
            f"coverage generation exited {proc.returncode} but produced no "
            f"coverage.json at {coverage_json}."
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
