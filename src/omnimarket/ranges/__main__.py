# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI: ``python -m omnimarket.ranges {check-register,evaluate,compare}``.

Exit codes. ``check-register``: 0 the register passes, 1 it does not.
``evaluate``: 0 MET, 1 MISSED, 2 REFUSED. A range check blocks, so every
outcome other than MET is non-zero. ``compare``: 0 NO_DIFFERENCE,
1 DIFFERENCE, 2 REFUSED.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from omnimarket.models.ranges import (
    EnumComparisonVerdict,
    EnumRangeVerdict,
    ModelComparisonMethod,
    ModelComparisonPair,
    ModelRangeAcceptanceLine,
    ModelRangeRun,
)
from omnimarket.ranges.compare import compare_paired_outcomes
from omnimarket.ranges.evaluate import evaluate_range_line
from omnimarket.ranges.register import (
    DEFAULT_CHECK_REGISTER_PATH,
    validate_check_register,
)


def _emit(text: str) -> None:
    sys.stdout.write(f"{text}\n")


_EVALUATE_EXIT = {
    EnumRangeVerdict.MET: 0,
    EnumRangeVerdict.MISSED: 1,
    EnumRangeVerdict.REFUSED: 2,
}


def _check_register(path: Path) -> int:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    errors = validate_check_register(document)
    if errors:
        _emit(f"check register FAILED: {path.name}: {len(errors)} error(s)")
        for error in errors:
            _emit(f"  - {error}")
        return 1
    count = len(document["checks"])
    _emit(f"check register OK: {path.name}: {count} check(s), every one classified")
    return 0


def _evaluate(line_path: Path, runs_path: Path) -> int:
    line = ModelRangeAcceptanceLine.model_validate(
        yaml.safe_load(line_path.read_text(encoding="utf-8"))
    )
    raw_runs = json.loads(runs_path.read_text(encoding="utf-8"))
    runs = [ModelRangeRun.model_validate(run) for run in raw_runs]
    evaluation = evaluate_range_line(line, runs)
    _emit(f"{evaluation.verdict.value.upper()} {evaluation.check_id}")
    _emit(json.dumps(evaluation.model_dump(mode="json"), indent=2, sort_keys=True))
    for reason in evaluation.reasons:
        _emit(f"  - {reason}")
    return _EVALUATE_EXIT[evaluation.verdict]


_COMPARE_EXIT = {
    EnumComparisonVerdict.NO_DIFFERENCE: 0,
    EnumComparisonVerdict.DIFFERENCE: 1,
    EnumComparisonVerdict.REFUSED: 2,
}


def _compare(method_path: Path, pairs_path: Path, comparison_id: str) -> int:
    method = ModelComparisonMethod.model_validate(
        yaml.safe_load(method_path.read_text(encoding="utf-8"))
    )
    raw_pairs = json.loads(pairs_path.read_text(encoding="utf-8"))
    pairs = [ModelComparisonPair.model_validate(pair) for pair in raw_pairs]
    result = compare_paired_outcomes(comparison_id, pairs, method)
    _emit(f"{result.verdict.value.upper()} {result.comparison_id}")
    _emit(json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True))
    for reason in result.reasons:
        _emit(f"  - {reason}")
    return _COMPARE_EXIT[result.verdict]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m omnimarket.ranges")
    commands = parser.add_subparsers(dest="command", required=True)
    register = commands.add_parser(
        "check-register", help="refuse a register entry with no class or no method"
    )
    register.add_argument(
        "path", nargs="?", type=Path, default=DEFAULT_CHECK_REGISTER_PATH
    )
    evaluate = commands.add_parser(
        "evaluate", help="judge runs against a range acceptance line"
    )
    evaluate.add_argument("--line", type=Path, required=True)
    evaluate.add_argument("--runs", type=Path, required=True)
    compare = commands.add_parser(
        "compare", help="judge rung B against rung A over the same graded prompts"
    )
    compare.add_argument("--method", type=Path, required=True)
    compare.add_argument("--pairs", type=Path, required=True)
    compare.add_argument("--comparison-id", default="comparison")
    args = parser.parse_args(argv)
    if args.command == "check-register":
        return _check_register(args.path)
    if args.command == "compare":
        return _compare(args.method, args.pairs, args.comparison_id)
    return _evaluate(args.line, args.runs)


if __name__ == "__main__":
    sys.exit(main())
