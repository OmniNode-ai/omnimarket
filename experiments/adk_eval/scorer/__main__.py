# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""CLI for the ADK-eval scorer (P9).

Usage:
    python -m experiments.adk_eval.scorer \\
        --track-a path/to/track_a_report.json \\
        --track-b path/to/track_b_report.json \\
        --output .onex_state/evidence/adk-eval/scores.json

Both --track-a and --track-b are optional; missing tracks are emitted as
zero-scored stubs with a caveat, so the scorer can be run end-to-end
before both tracks finish.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from experiments.adk_eval._local_models import ModelTypeDebtReport
from experiments.adk_eval.scorer.scorer import (
    load_labels,
    score_reports,
)

_DEFAULT_LABELS = Path(__file__).resolve().parents[1] / "eval" / "labeled_sample.yaml"


def _load_report_or_none(path: Path | None) -> ModelTypeDebtReport | None:
    if path is None:
        return None
    return ModelTypeDebtReport.model_validate_json(path.read_text())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="experiments.adk_eval.scorer",
        description="Score ADK-eval track reports against the P5 labeled sample.",
    )
    parser.add_argument(
        "--track-a",
        type=Path,
        default=None,
        help="Path to Track A (ADK) ModelTypeDebtReport JSON (optional).",
    )
    parser.add_argument(
        "--track-b",
        type=Path,
        default=None,
        help="Path to Track B (omnimarket POC) ModelTypeDebtReport JSON (optional).",
    )
    parser.add_argument(
        "--labels",
        type=Path,
        default=_DEFAULT_LABELS,
        help="Path to labeled_sample.yaml (default: P5 sample inside adk_eval/eval).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Where to write scores.json.",
    )
    args = parser.parse_args(argv)

    if not args.labels.exists():
        sys.stderr.write(f"ERROR: labels file not found: {args.labels}\n")
        return 2

    labels = load_labels(args.labels)
    track_a_report = _load_report_or_none(args.track_a)
    track_b_report = _load_report_or_none(args.track_b)

    scores = score_reports(
        track_a=track_a_report,
        track_b=track_b_report,
        labels=labels,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(scores, indent=2, sort_keys=True) + "\n")
    sys.stdout.write(f"Wrote scores to {args.output}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
