#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CI dependency health gate — run the dep-health sweep and enforce findings threshold.

Exit codes:
  0 — clean (no findings at or above threshold)
  1 — findings at or above threshold (when --exit-nonzero-on-findings is set)
  2 — AST fallback itself failed (unparseable Python or critical engine error)

Mirrors the structure of run_runtime_sweep.py. Designed for plain-Python CI
execution without the Claude Code harness.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Add src/ to path so omnimarket imports work in CI without editable install
_REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from omnimarket.nodes.node_dependency_health_sweep import (  # noqa: E402
    HandlerDepHealthSweep,
)
from omnimarket.nodes.node_dependency_health_sweep.models.model_dep_health_finding import (  # noqa: E402
    EnumDepHealthSeverity,
)
from omnimarket.nodes.node_dependency_health_sweep.models.model_dep_health_sweep_request import (  # noqa: E402
    ModelDepHealthSweepRequest,
)

_SEVERITY_ORDER = [
    EnumDepHealthSeverity.INFO,
    EnumDepHealthSeverity.MINOR,
    EnumDepHealthSeverity.MAJOR,
    EnumDepHealthSeverity.CRITICAL,
]


def _severity_index(severity_str: str) -> int:
    try:
        sev = EnumDepHealthSeverity(severity_str)
        return _SEVERITY_ORDER.index(sev)
    except (ValueError, AttributeError):
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-roots",
        required=True,
        help="Comma-separated list of repo root paths to scan.",
    )
    parser.add_argument(
        "--severity-threshold",
        default="MAJOR",
        choices=["INFO", "MINOR", "MAJOR", "CRITICAL"],
        help="Minimum severity to include in blocking count (default: MAJOR).",
    )
    parser.add_argument(
        "--baseline-path",
        default=None,
        help="Path to baseline JSON for delta-mode blocking.",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Stable run ID for idempotent baseline tracking.",
    )
    parser.add_argument(
        "--exit-nonzero-on-findings",
        action="store_true",
        help="Exit 1 if any findings at or above --severity-threshold are found.",
    )
    parser.add_argument(
        "--delta-mode",
        action="store_true",
        help=(
            "Exit 1 only when NEW findings appear vs baseline (baseline_delta > 0). "
            "Requires --baseline-path. Existing findings in baseline do not block."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run analysis but always exit 0 (advisory mode).",
    )

    args = parser.parse_args(argv)

    if args.delta_mode:
        if not args.baseline_path:
            print(
                "dep-health-gate: --delta-mode requires --baseline-path",
                file=sys.stderr,
            )
            return 2
        if not Path(args.baseline_path).exists():
            print(
                "dep-health-gate: --delta-mode requires an existing --baseline-path file",
                file=sys.stderr,
            )
            return 2

    repo_roots = [r.strip() for r in args.repo_roots.split(",") if r.strip()]

    request = ModelDepHealthSweepRequest(
        repo_roots=repo_roots,
        severity_threshold=args.severity_threshold,
        baseline_path=args.baseline_path,
        run_id=args.run_id,
        dry_run=args.dry_run,
    )

    handler = HandlerDepHealthSweep()

    try:
        result = handler.handle(request)
    except RuntimeError as exc:
        print(f"ERROR: dep-health sweep engine failed: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"ERROR: dep-health sweep unexpected failure: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(result.model_dump(mode="json"), indent=2, default=str))

    if args.dry_run:
        return 0

    if args.delta_mode:
        delta = result.baseline_delta
        if delta is None:
            print(
                "dep-health-gate: --delta-mode requires a valid --baseline-path with an existing baseline file",
                file=sys.stderr,
            )
            return 2
        if delta > 0:
            print(
                f"dep-health-gate: {delta} new finding(s) introduced vs baseline at or above {args.severity_threshold}",
                file=sys.stderr,
            )
            return 1
        return 0

    if args.exit_nonzero_on_findings:
        threshold_index = _severity_index(args.severity_threshold)
        blocking = [
            f
            for f in result.findings
            if _severity_index(f.severity.value) >= threshold_index
        ]
        if blocking:
            count = len(blocking)
            print(
                f"dep-health-gate: {count} finding(s) at or above {args.severity_threshold}",
                file=sys.stderr,
            )
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
