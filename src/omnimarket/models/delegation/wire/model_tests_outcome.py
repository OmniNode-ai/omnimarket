# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The typed outcome of a code answer's tests (OMN-26682).

A code answer that no test was named for or run against is accepted on its
contract checks alone. Its receipt must say ``untested``, never ``passed``:
passed means tests ran and passed.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from omnibase_core.models.delegation.wire.model_quality_gate import (
    ModelQualityRuleEvaluation,
)

from omnimarket.models.delegation.wire.model_quality_gate import (
    INSUFFICIENT_EVIDENCE_VERDICT_PREFIX,
    SCORE_VERIFYING_CHECKS,
    UNTESTED_VERDICT_PREFIX,
)

EnumTestsOutcome = Literal["passed", "failed", "untested", "not_run"]

# The rule the answer gate appends for a code answer's named tests, and the
# prefix of the reason it records when they ran and failed.
NAMED_TESTS_RULE = "named_tests_passed"
TESTS_FAILED_PREFIX = "TESTS_FAILED"

_TEST_RULES = frozenset({NAMED_TESTS_RULE, *SCORE_VERIFYING_CHECKS})


def tests_outcome_from_rules(
    rule_evaluations: Iterable[ModelQualityRuleEvaluation],
) -> EnumTestsOutcome | None:
    """The tests outcome the rule evaluations record; None when no test rule applies.

    An untested record wins over everything, then a not-run record (named tests
    that no runner ran): a code answer whose tests never ran is never reported
    passed. Failed beats passed.
    """
    outcomes: set[str] = set()
    for evaluation in rule_evaluations:
        if evaluation.rule not in _TEST_RULES:
            continue
        detail = evaluation.detail or ""
        if detail.startswith(f"{UNTESTED_VERDICT_PREFIX}:"):
            outcomes.add("untested")
        elif detail.startswith(INSUFFICIENT_EVIDENCE_VERDICT_PREFIX) and (
            not evaluation.passed
        ):
            # Tests were named but no run reached the gate (NO_NAMED_TEST_RUN_REASON).
            outcomes.add("not_run")
        elif evaluation.passed:
            outcomes.add("passed")
        elif detail.startswith(TESTS_FAILED_PREFIX):
            outcomes.add("failed")
    for outcome in ("untested", "not_run", "failed", "passed"):
        if outcome in outcomes:
            return outcome
    return None


__all__ = [
    "NAMED_TESTS_RULE",
    "TESTS_FAILED_PREFIX",
    "EnumTestsOutcome",
    "tests_outcome_from_rules",
]
