# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""CLI entrypoint for the ADK-eval measurement harness (P8).

Writes `measurements.json` into the shared evidence directory. Defaults
to the canonical `$OMNI_HOME/.onex_state/evidence/adk-eval/` location
(overridable via env var or flag) so Track A, Track B, scorer, and harness
all converge on a single artifact tree.

Dev-time rough wall-clock estimates (per P8 contract) are hardcoded here;
they trace back to the worker-dispatch runtime the team lead observed:
  - Track A: ~60 min (scaffold + 2 retries + commit)
  - Track B: ~40 min (infra reuse, faster path)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from omnimarket.experiments.adk_eval.harness.aggregator import aggregate

_DEFAULT_DEV_TIME_MINUTES_A = 60
_DEFAULT_DEV_TIME_MINUTES_B = 40


def _default_evidence_dir() -> Path:
    omni_home = os.environ.get("OMNI_HOME", str(Path.home() / "Code" / "omni_home"))
    return Path(omni_home) / ".onex_state" / "evidence" / "adk-eval"


def main(argv: list[str] | None = None) -> int:
    evidence_dir = _default_evidence_dir()

    parser = argparse.ArgumentParser(
        prog="omnimarket.experiments.adk_eval.harness",
        description=(
            "Aggregate ADK-eval track metrics + scorer output into "
            "measurements.json for the P10 Decision Gate."
        ),
    )
    parser.add_argument(
        "--track-a-metrics",
        type=Path,
        default=evidence_dir / "track_a_metrics.json",
    )
    parser.add_argument(
        "--track-b-metrics",
        type=Path,
        default=evidence_dir / "track_b_metrics.json",
    )
    parser.add_argument(
        "--scores",
        type=Path,
        default=evidence_dir / "scores.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=evidence_dir / "measurements.json",
    )
    parser.add_argument(
        "--dev-time-minutes-a",
        type=int,
        default=_DEFAULT_DEV_TIME_MINUTES_A,
    )
    parser.add_argument(
        "--dev-time-minutes-b",
        type=int,
        default=_DEFAULT_DEV_TIME_MINUTES_B,
    )
    args = parser.parse_args(argv)

    for required in (args.track_a_metrics, args.track_b_metrics, args.scores):
        if not required.exists():
            sys.stderr.write(f"ERROR: required input missing: {required}\n")
            return 2

    result = aggregate(
        track_a_metrics_path=args.track_a_metrics,
        track_b_metrics_path=args.track_b_metrics,
        scores_path=args.scores,
        dev_time_minutes_a=args.dev_time_minutes_a,
        dev_time_minutes_b=args.dev_time_minutes_b,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    sys.stdout.write(f"Wrote measurements to {args.output}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
