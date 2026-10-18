# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13850: ``passes_existing_tests`` is honest — no silent always-pass.

Before this ticket ``passes_existing_tests`` — a declared deterministic DoD check
for ``code_generation`` / ``validator_generation`` — was aliased to
``_check_response_non_empty`` in the quality-gate reducer. It could never fail on
any non-empty answer, so it contributed a false "passed" to the deterministic
fraction while verifying nothing about test execution (a tautology-class
truth-integrity defect).

This suite pins honest test evidence under OMN-17427:

  * A request naming no tests records UNTESTED, skipped and never passed.
  * Named tests without a run are refused as INSUFFICIENT, with a failed
    ``passes_existing_tests`` evaluation rather than a skipped check.
  * The deterministic fraction (``actual_score``) includes a named-test
    missing-run failure; UNTESTED checks are excluded from the fraction.
  * The empty/refusal deterministic HARD FLOOR still hard-blocks an empty answer,
    independently of the missing execution evidence.
  * A real deterministic check still produces a real pass and a real fail
    alongside a named-test missing-run failure.

Execution of named tests is an EFFECT boundary; this reducer stays pure and
reads the execution evidence supplied by its caller.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from omnimarket.models.delegation.wire.model_quality_gate import (
    NO_NAMED_TEST_RUN_REASON,
    NO_NAMED_TESTS_UNTESTED_REASON,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)

_NODE_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_delegation_quality_gate_reducer"
    / "contract.yaml"
)

# A clean, compiling, single-artifact code answer (deterministic floor passes).
_GOOD_CODE = "```python\ndef add(a: int, b: int) -> int:\n    return a + b\n```"
# A syntactically invalid code answer — fails the evaluated ``compiles_without_errors``.
_BROKEN_CODE = "```python\ndef add(a: int, b: int) -> int\n    return a + b\n```"


def _gate(
    content: str,
    *,
    deterministic: tuple[str, ...],
    heuristic: tuple[str, ...] = (),
    task_type: str = "code_generation",
) -> ModelQualityGateInput:
    return ModelQualityGateInput(
        correlation_id=uuid4(),
        task_type=task_type,
        llm_response_content=content,
        dod_deterministic=deterministic,
        dod_heuristic=heuristic,
    )


@pytest.mark.unit
def test_passes_existing_tests_with_no_named_tests_is_recorded_untested() -> None:
    """No tests named: accept the contract and record no test pass."""
    result = delta(
        _gate(
            _GOOD_CODE,
            deterministic=("compiles_without_errors", "passes_existing_tests"),
        )
    )
    assert result.passed is True
    assert result.fail_category == "pass"
    assert result.skipped_checks == ("passes_existing_tests",)
    assert result.failure_reasons == ()
    assert result.failure_cases == ()
    assert result.no_rung_can_satisfy is False
    assert result.actual_score == 1.0  # Only the compilation check was scored.
    assert result.score_verified is False
    evaluation = next(
        e for e in result.rule_evaluations if e.rule == "passes_existing_tests"
    )
    assert evaluation.enforcement == "scored"
    assert evaluation.passed is False
    assert evaluation.detail == NO_NAMED_TESTS_UNTESTED_REASON


@pytest.mark.unit
def test_missing_run_check_never_reports_passed_alone() -> None:
    """A lone named-test check without a run cannot accept a clean answer.

    Tests were named and ``passes_existing_tests`` is the only declared check.
    It runs nothing, so it confers no acceptance authority — the clean answer is NOT
    accepted (the old phantom returned ``passed=True`` here).
    """
    result = delta(
        _gate(_GOOD_CODE, deterministic=("passes_existing_tests",)),
        named_tests_requested=True,
    )
    assert result.passed is False
    # OMN-21077: missing execution is a scored deterministic failure.
    assert result.fail_category == "fail_deterministic"
    assert result.actual_score == 0.0
    assert result.score_source == "deterministic_acceptance"
    assert result.failure_reasons == (NO_NAMED_TEST_RUN_REASON,)
    assert result.no_rung_can_satisfy is True


@pytest.mark.unit
def test_deterministic_fraction_includes_the_missing_run_failure() -> None:
    """Two passing checks and one missing-run failure score 2/3, not 2/2."""
    result = delta(
        _gate(
            _GOOD_CODE,
            deterministic=(
                "compiles_without_errors",
                "final_artifact_only",
                "passes_existing_tests",
            ),
        ),
        named_tests_requested=True,
    )
    # OMN-21077: the required missing run contributes a failure to the fraction.
    assert result.passed is False
    assert result.fail_category == "fail_deterministic"
    assert result.actual_score == pytest.approx(0.667)
    assert result.skipped_checks == ()
    assert result.failure_reasons == (NO_NAMED_TEST_RUN_REASON,)


@pytest.mark.unit
def test_real_evaluated_fail_scores_alongside_missing_run_failure() -> None:
    """Broken syntax and missing execution fail two of the three checks."""
    result = delta(
        _gate(
            _BROKEN_CODE,
            deterministic=(
                "compiles_without_errors",
                "final_artifact_only",
                "passes_existing_tests",
            ),
        ),
        named_tests_requested=True,
    )
    assert result.passed is False
    assert result.fail_category == "fail_deterministic"
    # OMN-21077: both the syntax failure and the missing run count against code.
    assert result.actual_score == pytest.approx(0.333)
    assert result.skipped_checks == ()
    assert any("compile" in r for r in result.failure_reasons)
    assert NO_NAMED_TEST_RUN_REASON in result.failure_reasons


@pytest.mark.unit
def test_empty_hard_floor_survives_missing_passes_existing_tests() -> None:
    """MUST-NOT-change: the empty/refusal deterministic hard floor still blocks.

    Before OMN-13850 the ONLY thing rejecting an EMPTY code_generation answer on
    the deterministic path was ``passes_existing_tests`` aliased to
    ``_check_response_non_empty``. With that alias removed, an always-applied
    empty floor keeps the empty answer hard-blocking — because it is EMPTY, not
    because a phantom test check "failed".
    """
    result = delta(
        _gate(
            "",
            deterministic=(
                "compiles_without_errors",
                "final_artifact_only",
                "passes_existing_tests",
            ),
            heuristic=("no_refusal",),
        )
    )
    assert result.passed is False
    assert result.fail_category == "fail_deterministic"
    assert any("empty response" in r for r in result.failure_reasons)


@pytest.mark.unit
def test_empty_floor_not_double_counted_with_declared_response_non_empty() -> None:
    """The empty floor de-duplicates against a declared ``response_non_empty``.

    ``validator_generation`` declares ``passes_existing_tests``; a set that ALSO
    declares ``response_non_empty`` must not emit two ``MALFORMED: empty response``
    reasons for a single empty answer.
    """
    result = delta(
        _gate(
            "",
            task_type="validator_generation",
            deterministic=(
                "response_non_empty",
                "compiles_without_errors",
                "passes_existing_tests",
            ),
        )
    )
    empty_reasons = [r for r in result.failure_reasons if "empty response" in r]
    assert len(empty_reasons) == 1
    # The empty floor blocks independently of the skipped UNTESTED check.
    assert "passes_existing_tests" in result.skipped_checks
    assert NO_NAMED_TEST_RUN_REASON not in result.failure_reasons
    evaluation = next(
        e for e in result.rule_evaluations if e.rule == "passes_existing_tests"
    )
    assert evaluation.enforcement == "scored"
    assert evaluation.passed is False
    assert evaluation.detail == NO_NAMED_TESTS_UNTESTED_REASON
    assert result.passed is False
    assert result.fail_category == "fail_deterministic"


@pytest.mark.unit
def test_judge_combine_cannot_mask_empty_without_test_run() -> None:
    """A maximal judge score cannot lift an empty answer over the bar.

    The empty floor is a deterministic hard block that fires BEFORE any judge
    combine, so ``score_source`` never becomes ``"combined"`` for an empty answer
    even with the ``passes_existing_tests`` phantom removed.
    """
    result = delta(
        _gate(
            "",
            deterministic=(
                "compiles_without_errors",
                "final_artifact_only",
                "passes_existing_tests",
            ),
            heuristic=("no_refusal",),
        ),
        judge_adequacy_score=0.99,
    )
    assert result.passed is False
    assert result.fail_category == "fail_deterministic"
    assert result.score_source != "combined"


def _fsm_states() -> tuple[str, ...]:
    """Declared FSM state names from the node's contract (runtime-derived)."""
    contract = yaml.safe_load(_NODE_CONTRACT.read_text(encoding="utf-8"))
    state_machine = contract["state_machine"]
    return tuple(s["state_name"] for s in state_machine["states"])


def _post_request_to_state() -> str:
    """The FSM state reached when the reducer folds a ``quality_gate_request``."""
    contract = yaml.safe_load(_NODE_CONTRACT.read_text(encoding="utf-8"))
    state_machine = contract["state_machine"]
    initial = state_machine["initial_state"]
    for transition in state_machine["transitions"]:
        if (
            transition["from_state"] == initial
            and transition["trigger"] == "quality_gate_request"
        ):
            to_state: str = transition["to_state"]
            return to_state
    raise AssertionError("no quality_gate_request transition from the initial state")


@pytest.mark.unit
def test_gate_evaluation_lands_in_the_evaluated_fsm_state() -> None:
    """OMN-13850 / OMN-13781: a folded gate evaluation materializes ``evaluated``.

    The reducer's ``evaluated`` FSM state ("Quality-gate result projection
    materialized") is the state reached after folding a ``quality_gate_request``
    that carries a ``delta``-produced result. This ties the declared state to real
    evaluation behavior and pays down the node's baselined state-coverage gap: a
    real gate evaluation (below) is exactly what drives the transition INTO
    ``"evaluated"``.
    """
    # A real gate evaluation produces the result that a quality_gate_request folds.
    result = delta(
        _gate(
            _GOOD_CODE,
            deterministic=("compiles_without_errors", "final_artifact_only"),
        )
    )
    assert result.passed is True

    declared_states = _fsm_states()
    # ``evaluated`` is a declared, non-terminal projection state...
    assert "evaluated" in declared_states
    # ...and it is precisely the state a folded quality_gate_request transitions
    # into from ``idle`` (the state where the gate result is materialized).
    assert _post_request_to_state() == "evaluated"
