# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13850: ``passes_existing_tests`` is honest — no silent always-pass.

Before this ticket ``passes_existing_tests`` — a declared deterministic DoD check
for ``code_generation`` / ``validator_generation`` — was aliased to
``_check_response_non_empty`` in the quality-gate reducer. It could never fail on
any non-empty answer, so it contributed a false "passed" to the deterministic
fraction while verifying nothing about test execution (a tautology-class
truth-integrity defect).

This suite pins the honest behavior:

  * SKIPPED-when-unevaluated: with no wired acceptance-command executor the check
    is recorded as SKIPPED, confers no acceptance authority, and is EXCLUDED from
    the deterministic passed/total fraction — it never reports "passed".
  * The deterministic fraction (``actual_score``) reflects ONLY evaluated checks,
    so a skipped check cannot inflate the score.
  * The empty/refusal deterministic HARD FLOOR (MUST-NOT-change) still hard-blocks
    an empty answer on the verifiable path, independently of the (now skipped)
    ``passes_existing_tests`` check.
  * A real evaluated deterministic check still produces a real pass and a real
    fail alongside the skipped check.

Execution of the acceptance command (real test-suite run) is an EFFECT boundary
and is a scoped follow-up (see the OMN-13850 PR body); this reducer stays pure,
so ``passes_existing_tests`` is SKIPPED here rather than executed.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
import yaml

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
def test_passes_existing_tests_is_recorded_as_skipped() -> None:
    """The unevaluated check surfaces in ``skipped_checks``, not as a pass/fail."""
    result = delta(
        _gate(
            _GOOD_CODE,
            deterministic=("compiles_without_errors", "passes_existing_tests"),
        )
    )
    assert "passes_existing_tests" in result.skipped_checks
    # It is neither a failure reason nor a failure case.
    assert not any("passes_existing_tests" in r for r in result.failure_reasons)
    assert not any("passes_existing_tests" in c for c in result.failure_cases)


@pytest.mark.unit
def test_skipped_check_never_reports_passed_alone() -> None:
    """OMN-13850 core: a lone SKIPPED check cannot promote a clean answer to pass.

    ``passes_existing_tests`` is the only declared deterministic check. It runs
    nothing, so it confers no acceptance authority — the clean answer is NOT
    accepted (the old phantom returned ``passed=True`` here).
    """
    result = delta(_gate(_GOOD_CODE, deterministic=("passes_existing_tests",)))
    assert result.passed is False
    assert result.fail_category == "fail_heuristic"
    # No deterministic-acceptance evidence is produced — nothing was evaluated.
    assert result.actual_score is None
    assert result.score_source == ""


@pytest.mark.unit
def test_deterministic_fraction_excludes_the_skipped_check() -> None:
    """``actual_score`` counts only EVALUATED checks, never the skipped one.

    With two evaluated checks passing (``compiles_without_errors``,
    ``final_artifact_only``) plus one skipped (``passes_existing_tests``), the
    deterministic fraction is 2/2 = 1.0 — the skipped check is NOT in the
    denominator. A phantom always-pass would have counted 3/3 the same way, but a
    phantom FAILURE-masking check would have hidden a real miss; excluding it keeps
    the denominator honest.
    """
    result = delta(
        _gate(
            _GOOD_CODE,
            deterministic=(
                "compiles_without_errors",
                "final_artifact_only",
                "passes_existing_tests",
            ),
        )
    )
    assert result.passed is True
    assert result.actual_score == pytest.approx(1.0)
    assert result.skipped_checks == ("passes_existing_tests",)


@pytest.mark.unit
def test_real_evaluated_fail_scores_alongside_skipped_check() -> None:
    """A real evaluated deterministic failure is scored on the EVALUATED total.

    ``compiles_without_errors`` fails (broken syntax); ``final_artifact_only``
    passes; ``passes_existing_tests`` is skipped. Evaluated total = 2, one failed,
    so the deterministic fraction is 1/2 = 0.5 — NOT diluted to 2/3 by counting the
    skipped check as a denominator term.
    """
    result = delta(
        _gate(
            _BROKEN_CODE,
            deterministic=(
                "compiles_without_errors",
                "final_artifact_only",
                "passes_existing_tests",
            ),
        )
    )
    assert result.passed is False
    assert result.fail_category == "fail_deterministic"
    assert result.actual_score == pytest.approx(0.5)
    assert result.skipped_checks == ("passes_existing_tests",)
    assert any("compile" in r for r in result.failure_reasons)


@pytest.mark.unit
def test_empty_hard_floor_survives_skipping_passes_existing_tests() -> None:
    """MUST-NOT-change: the empty/refusal deterministic hard floor still blocks.

    Before OMN-13850 the ONLY thing rejecting an EMPTY code_generation answer on
    the deterministic path was ``passes_existing_tests`` aliased to
    ``_check_response_non_empty``. Now that the check is SKIPPED, an always-applied
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
    assert "passes_existing_tests" in result.skipped_checks


@pytest.mark.unit
def test_judge_combine_cannot_mask_empty_after_skip() -> None:
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
