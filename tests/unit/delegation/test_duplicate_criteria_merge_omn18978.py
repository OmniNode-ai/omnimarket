# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""A criterion the task class already declares must not duplicate a rule (OMN-18978).

THE DEFECT, and it stopped delegation across the fleet. Caller-supplied
`--criteria` were CONCATENATED onto the task class's declared definition of
done rather than unioned by rule name. When a caller named a criterion the
class already declares, the same rule was evaluated twice, and
`ModelDelegationResult`'s own validator refuses that: "rule_evaluations must
record each rule at most once". The failed terminal therefore could not be
CONSTRUCTED, so nothing was published, and the caller waited out the full
240-second handler budget before a synthesized timeout arrived carrying
`attempts: []`.

Measured end to end on the dev lane, correlation
`e379a4b9-8fbc-4408-b277-07ec32ac1876`: five rungs ran and five models
answered in 37 seconds, every rung was refused at 0.373 against a bar of
0.800, and then the runtime logged

    ValidationError: 1 validation error for ModelDelegationFailed
    Value error, rule_evaluations must record each rule at most once

at the exact second of the final decision, followed by 196 seconds of silence.

WHY THE DUPLICATION IS ACROSS THE TWO SETS, not within one. The caller's
criteria are appended to `dod_deterministic`, while `test` declares
`no_refusal` and `covers_edge_cases` as `dod_heuristic`. So a caller naming
either one produces a rule that is evaluated once as deterministic and once
as heuristic. Deduplicating `acceptance_criteria` against itself, or against
`dod_deterministic` alone, does not close it -- which is why the parametrized
case below covers both sets.

WHAT THE CALLER DID WRONG: nothing. Naming a criterion the class also cares
about is reasonable, and the same collision can arise between two callers'
own criteria. The union belongs at the merge.
"""

from __future__ import annotations

import pytest

from omnimarket.models.delegation.wire.model_quality_gate import (
    ModelQualityGateInput,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta,
)

pytestmark = pytest.mark.unit

#: The `test` task class's declared definition of done, as the contract
#: spells it. Written out rather than loaded so this test states the shape it
#: is about; the live contract is pinned elsewhere.
_TEST_CLASS_DETERMINISTIC = (
    "compiles_without_errors",
    "final_artifact_only",
    "uses_pytest_mark_unit",
)
_TEST_CLASS_HEURISTIC = ("no_refusal", "covers_edge_cases", "covers_error_paths")


def _gate_input(criteria: tuple[str, ...]) -> ModelQualityGateInput:
    """The gate input a `--task-type test --criteria ...` run produces."""
    return ModelQualityGateInput(
        correlation_id="e379a4b9-8fbc-4408-b277-07ec32ac1876",
        task_type="test",
        llm_response_content=(
            "This change makes a receipt-mode evidence requirement conditional "
            "on what the request asked for, instead of demanding it of every "
            "completed terminal."
        ),
        dod_deterministic=_TEST_CLASS_DETERMINISTIC,
        dod_heuristic=_TEST_CLASS_HEURISTIC,
        acceptance_criteria=criteria,
    )


def _rule_names(result: object) -> list[str]:
    return [evaluation.rule for evaluation in result.rule_evaluations]  # type: ignore[attr-defined]


class TestTheRuleSetCarriesEachRuleOnce:
    """AC1. The invariant the terminal model enforces, enforced at the source."""

    @pytest.mark.parametrize(
        ("criteria", "why"),
        [
            (
                ("covers_edge_cases", "no_refusal"),
                "the exact invocation that hung the lane; both duplicate HEURISTIC rules",
            ),
            (
                ("compiles_without_errors",),
                "a criterion duplicating a DETERMINISTIC rule",
            ),
            (
                ("no_refusal", "no_refusal"),
                "a caller repeating itself, which needs no class overlap at all",
            ),
            (
                ("covers_edge_cases", "compiles_without_errors"),
                "one of each, so a fix that closes only one set still fails",
            ),
        ],
        ids=[
            "both duplicate heuristic rules",
            "duplicates a deterministic rule",
            "the caller repeats itself",
            "one from each set",
        ],
    )
    def test_no_rule_is_evaluated_twice(
        self, criteria: tuple[str, ...], why: str
    ) -> None:
        names = _rule_names(delta(_gate_input(criteria)))
        duplicates = sorted({name for name in names if names.count(name) > 1})
        assert not duplicates, f"{why}: {duplicates} in {names}"

    def test_a_disjoint_criterion_is_still_added(self) -> None:
        """Positive control: dedup must not become "drop the caller's criteria".

        A fix that silently discarded `acceptance_criteria` would pass every
        case above and defeat the feature.
        """
        names = _rule_names(delta(_gate_input(("no_obvious_regressions",))))
        assert "no_obvious_regressions" in names

    def test_the_declared_rules_all_survive(self) -> None:
        """The class's own definition of done is not lost to the dedup."""
        names = set(_rule_names(delta(_gate_input(("covers_edge_cases",)))))
        for rule in _TEST_CLASS_DETERMINISTIC + _TEST_CLASS_HEURISTIC:
            assert rule in names, rule


class TestTheResultCanBecomeATerminal:
    """AC1, stated as the thing that actually broke rather than as a rule count.

    A unique rule list is only interesting because the terminal model refuses
    a duplicated one. This asserts the real constraint, so the test stays
    honest if the rule set is ever carried differently.
    """

    def test_the_gate_result_satisfies_the_terminal_validator(self) -> None:
        from omnibase_core.models.delegation.wire.model_delegation_result import (
            ModelDelegationResult,
        )

        result = delta(_gate_input(("covers_edge_cases", "no_refusal")))
        # The validator that refused the live terminal lives on this model and
        # fires on construction; building one is the assertion.
        ModelDelegationResult.model_validate(
            {
                "correlation_id": "e379a4b9-8fbc-4408-b277-07ec32ac1876",
                "task_type": "test",
                "model_used": "qwen3.8-27b",
                "endpoint_url": "http://local.test:8000/v1/chat/completions",
                "content": "any",
                "quality_passed": False,
                "quality_score": 0.373,
                "latency_ms": 37_000,
                "fallback_to_claude": False,
                "rule_evaluations": [
                    evaluation.model_dump(mode="json")
                    for evaluation in result.rule_evaluations
                ],
            }
        )


class TestWhereADuplicatedRuleLands:
    """AC1, second half. Dedup must not silently re-tier a rule.

    `enforcement` decides whether a rule vetoes acceptance outright or only
    moves the graded score. Collapsing a duplicate is only safe if it leaves
    that answer where the task class put it.
    """

    def _enforcement(self, criteria: tuple[str, ...], rule: str) -> str:
        for evaluation in delta(_gate_input(criteria)).rule_evaluations:
            if evaluation.rule == rule:
                return str(evaluation.enforcement.value)
        raise AssertionError(f"{rule} was not evaluated at all")

    def test_naming_a_heuristic_rule_does_not_promote_it_to_blocking(self) -> None:
        """A caller names a rule, never a tier.

        Promoting on mention would let any caller make the bar stricter than
        the contract its response is graded against, silently.
        """
        assert self._enforcement(("covers_edge_cases",), "covers_edge_cases") == (
            self._enforcement((), "covers_edge_cases")
        )

    def test_a_disjoint_criterion_is_blocking(self) -> None:
        """Unchanged behaviour: a criterion naming no declared rule blocks."""
        assert (
            self._enforcement(("no_obvious_regressions",), "no_obvious_regressions")
            == "blocking"
        )
