# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seven-day count of accepted code answers governed by a response contract.

Source: the delegation run store, ``<state root>/runs/<run>/receipt.json`` and
``run.json``. A run counts when its receipt is newer than the window, its task
type is ``code_generation``, its status is ``success`` and its receipt carries
``response_contract_evidence``. Such a run is untested when its receipt records no
passed test rule (or records the UNTESTED verdict).
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omnimarket.models.delegation.wire.model_quality_gate import (
    SCORE_VERIFYING_CHECKS,
    UNTESTED_VERDICT_PREFIX,
)

WINDOW_SECONDS = 7 * 86400
_TEST_RULES = frozenset({"named_tests_passed", *SCORE_VERIFYING_CHECKS})


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    return data if isinstance(data, dict) else {}


@dataclass(frozen=True)
class GovernedCodeCounts:
    """Accepted contract-governed code answers, and how many of them ran no test."""

    governed: int
    untested: int


def _rule_evaluations(node: Any) -> list[dict[str, Any]]:
    """Every ``rule_evaluations`` list anywhere in a receipt, flattened."""
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "rule_evaluations" and isinstance(value, list):
                found.extend(item for item in value if isinstance(item, dict))
            else:
                found.extend(_rule_evaluations(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_rule_evaluations(item))
    return found


def _is_untested(receipt: dict[str, Any]) -> bool:
    """A run is tested only when a test rule is recorded passed, not untested."""
    for rule in _rule_evaluations(receipt):
        if rule.get("rule") not in _TEST_RULES:
            continue
        if str(rule.get("detail") or "").startswith(f"{UNTESTED_VERDICT_PREFIX}:"):
            return True
        if rule.get("passed") is True:
            return False
    return True


def count_governed_code_answers_by_outcome(
    runs_dir: Path, *, now: float | None = None, window_seconds: int = WINDOW_SECONDS
) -> GovernedCodeCounts:
    """Count accepted contract-governed code answers in the window, and the untested."""
    current = time.time() if now is None else now
    governed = 0
    untested = 0
    for run in sorted(runs_dir.glob("*")):
        receipt_path = run / "receipt.json"
        if not receipt_path.exists():
            continue
        if current - receipt_path.stat().st_mtime >= window_seconds:
            continue
        receipt = _read(receipt_path)
        if (
            _read(run / "run.json").get("task_type") == "code_generation"
            and receipt.get("status") == "success"
            and receipt.get("response_contract_evidence")
        ):
            governed += 1
            untested += _is_untested(receipt)
    return GovernedCodeCounts(governed=governed, untested=untested)


def count_governed_code_answers(
    runs_dir: Path, *, now: float | None = None, window_seconds: int = WINDOW_SECONDS
) -> int:
    """Count accepted code answers in the window that carry contract evidence."""
    return count_governed_code_answers_by_outcome(
        runs_dir, now=now, window_seconds=window_seconds
    ).governed


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        sys.stderr.write("usage: governed_code_count <runs directory>\n")
        return 2
    runs_dir = Path(args[0])
    counts = count_governed_code_answers_by_outcome(runs_dir)
    sys.stdout.write(f"accepted_code_governed_by_contract={counts.governed}\n")
    sys.stdout.write(f"accepted_code_governed_untested={counts.untested}\n")
    sys.stdout.write(f"source={runs_dir}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
