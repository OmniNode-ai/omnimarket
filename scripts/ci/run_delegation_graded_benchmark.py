#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delegation graded ladder benchmark report (OMN-13369, operator plan §3.6).

Escalating-complexity graded benchmark across the EXISTING local delegation
ladder — including the 5090/4090 AI-PC rungs and the DeepSeek-V4-Flash ceiling.
This SUPERSEDES the earlier fixture-content smoke replay: instead of scoring
hand-authored attempt text through the quality gate, it grades GENUINE recorded
per-rung model outputs with objective, deterministic graders and proves the
acceptance criterion — the floor rung scores measurably below the ceiling rung
(separation). Paid-cloud ceiling is deferred per the operator decision-of-record.

The recorded per-rung outputs are captured by
``scripts/ci/record_delegation_ladder_fixtures.py`` against the live endpoints;
this report and the CI test grade those committed fixtures hermetically.

Usage:
    uv run python scripts/ci/run_delegation_graded_benchmark.py
    uv run python scripts/ci/run_delegation_graded_benchmark.py --json
    uv run python scripts/ci/run_delegation_graded_benchmark.py --emit /tmp/p4.json
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from omnimarket.delegation.graded_ladder.harness import build_benchmark_packet


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json", action="store_true", help="print benchmark evidence packet JSON"
    )
    parser.add_argument("--emit", type=Path, help="write evidence packet JSON to path")
    args = parser.parse_args(argv)

    packet = build_benchmark_packet()

    if args.emit:
        args.emit.parent.mkdir(parents=True, exist_ok=True)
        args.emit.write_text(packet.model_dump_json(indent=2) + "\n")
    if args.json:
        print(packet.model_dump_json(indent=2))
    else:
        sep = packet.separation
        print(
            f"[delegation-graded-ladder] {packet.ticket} - {len(packet.rungs)} rungs, "
            f"{packet.n_tasks} tasks, tiers={packet.tiers}"
        )
        for score in sorted(packet.rung_scores, key=lambda s: s.order):
            print(
                f"  rung[{score.order}] {score.rung_id} ({score.model_name}, "
                f"{score.gpu}): pass={score.tasks_passed}/{score.tasks_total} "
                f"weighted={score.weighted_score} per_tier={score.per_tier_pass_rate}"
            )
        if sep is not None:
            print(
                f"  SEPARATION: floor {sep.floor_rung_id}={sep.floor_score} -> "
                f"ceiling {sep.ceiling_rung_id}={sep.ceiling_score} "
                f"margin={sep.margin} (required >= {sep.required_margin}) "
                f"monotonic={sep.monotonic_nondecreasing}"
            )
        if packet.passed:
            print("  PASS: graded benchmark separates the ladder (floor < ceiling)")
        else:
            for failure in packet.failures:
                print(f"  FAIL: {failure}")

    return 0 if packet.passed else 1


if __name__ == "__main__":
    sys.exit(main())
