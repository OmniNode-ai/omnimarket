#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Quality-gate calibration CI gate (OMN-12964, plan P1.7).

ENFORCEMENT RATCHET. This gate proves the delegation quality gate produces a
NON-DEGENERATE score distribution and that known-good outputs out-score
known-bad outputs across the calibration corpus.

Before OMN-12964 the gate returned a degenerate {0.0, 1.0} verdict and applied
code-docstring DoD to prose `document` tasks, so every output scored 0.000 on
both tiers (live CID a604cd40). A scoring artifact then masqueraded as an ON/OFF
effect, invalidating the interpretation of Experiments 1-3 (OMN-12944 / P0.2).

The gate FAILS (exit 1) when the corpus scores collapse, when the score range
falls below the discrimination threshold, when fewer than the required distinct
score values appear, when mean(good) does not clear mean(bad) by the required
margin, or when the corpus DoD has drifted from the shipped task-class contract.

Usage:
    uv run python scripts/ci/run_quality_gate_calibration.py            # enforce
    uv run python scripts/ci/run_quality_gate_calibration.py --json     # print packet
    uv run python scripts/ci/run_quality_gate_calibration.py --emit P   # write packet to P
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta as quality_gate_delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CORPUS_PATH = (
    _REPO_ROOT
    / "tests"
    / "unit"
    / "delegation"
    / "quality_gate_calibration_corpus.yaml"
)
_CONTRACT_PATH = (
    _REPO_ROOT / "src" / "omnimarket" / "configs" / "task_class_contracts.v1.yaml"
)

# Thresholds mirror tests/unit/delegation/test_quality_gate_calibration.py.
MIN_SCORE_RANGE = 0.3
MIN_DISTINCT_SCORES = 4
MIN_GOOD_BAD_MARGIN = 0.2


def _score_case(case: dict[str, Any], task_classes: dict[str, Any]) -> float:
    dod = task_classes[case["task_class"]]
    result = quality_gate_delta(
        ModelQualityGateInput(
            correlation_id=uuid4(),
            task_type=str(case["task_class"]),
            llm_response_content=str(case["content"]),
            dod_deterministic=tuple(dod.get("deterministic", ())),
            dod_heuristic=tuple(dod.get("heuristic", ())),
        )
    )
    return result.quality_score


def _build_packet() -> dict[str, Any]:
    corpus = yaml.safe_load(_CORPUS_PATH.read_text())
    contract = yaml.safe_load(_CONTRACT_PATH.read_text())["task_classes"]
    task_classes = corpus["task_classes"]

    rows: list[dict[str, Any]] = []
    scores: list[float] = []
    good: list[float] = []
    bad: list[float] = []
    for case in corpus["cases"]:
        score = _score_case(case, task_classes)
        label = str(case["label"])
        rows.append(
            {
                "id": str(case["id"]),
                "label": label,
                "task_class": str(case["task_class"]),
                "score": score,
            }
        )
        scores.append(score)
        if label == "good":
            good.append(score)
        elif label == "bad":
            bad.append(score)

    distinct = sorted({round(s, 3) for s in scores})

    failures: list[str] = []
    if len(distinct) < MIN_DISTINCT_SCORES:
        failures.append(
            f"degenerate distribution: only {len(distinct)} distinct scores "
            f"{distinct} (need >= {MIN_DISTINCT_SCORES})"
        )
    score_range = (max(scores) - min(scores)) if scores else 0.0
    if score_range < MIN_SCORE_RANGE:
        failures.append(f"score range {score_range:.3f} below {MIN_SCORE_RANGE}")
    if good and bad:
        margin = statistics.mean(good) - statistics.mean(bad)
        if margin < MIN_GOOD_BAD_MARGIN:
            failures.append(
                f"mean(good)-mean(bad)={margin:.3f} below {MIN_GOOD_BAD_MARGIN}"
            )
        if min(good) <= max(bad):
            failures.append(
                f"band overlap: worst good {min(good):.3f} <= best bad {max(bad):.3f}"
            )
    else:
        failures.append("corpus missing good or bad cases")

    for name, dod in task_classes.items():
        shipped = contract[name]["definition_of_done"]
        if list(dod["deterministic"]) != list(shipped["deterministic"]) or list(
            dod["heuristic"]
        ) != list(shipped["heuristic"]):
            failures.append(f"corpus DoD for '{name}' drifted from shipped contract")

    return {
        "ticket": "OMN-12964",
        "gate": "quality_gate_calibration",
        "thresholds": {
            "min_score_range": MIN_SCORE_RANGE,
            "min_distinct_scores": MIN_DISTINCT_SCORES,
            "min_good_bad_margin": MIN_GOOD_BAD_MARGIN,
        },
        "summary": {
            "n_cases": len(rows),
            "distinct_scores": distinct,
            "score_range": round(score_range, 3),
            "mean_good": round(statistics.mean(good), 3) if good else None,
            "mean_bad": round(statistics.mean(bad), 3) if bad else None,
        },
        "cases": rows,
        "failures": failures,
        "passed": not failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json", action="store_true", help="print evidence packet JSON"
    )
    parser.add_argument("--emit", type=Path, help="write evidence packet JSON to path")
    args = parser.parse_args()

    packet = _build_packet()

    if args.emit:
        args.emit.parent.mkdir(parents=True, exist_ok=True)
        args.emit.write_text(json.dumps(packet, indent=2) + "\n")
    if args.json:
        print(json.dumps(packet, indent=2))
    else:
        s = packet["summary"]
        print(f"[quality-gate-calibration] OMN-12964 - {s['n_cases']} cases")
        print(
            f"  distinct={s['distinct_scores']} range={s['score_range']} "
            f"mean_good={s['mean_good']} mean_bad={s['mean_bad']}"
        )
        if packet["passed"]:
            print(
                "  PASS: quality score distribution is non-degenerate and discriminating"
            )
        else:
            for f in packet["failures"]:
                print(f"  FAIL: {f}")

    return 0 if packet["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
