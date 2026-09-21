# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""A caller criterion must never become a bar no answer can pass (OMN-19005).

Two correct behaviours combined into an unpassable bar.

A response-shape directive in the prompt REPLACES the heuristic band for that
request, so a caller who wrote "answer with one word only" is not graded
against a prose rubric. Separately, a caller's acceptance criteria are unioned
into the DETERMINISTIC band unless the class already declares the name, so an
explicit ask is actually checked.

Together, a rule the class declared ONLY as heuristic stops being declared once
the override replaces that band. The criterion is then not "already declared",
lands in the deterministic band, and the deterministic chain has no arm for it,
so the gate reports `MALFORMED: unsupported deterministic DoD check`.

**Nothing a model can write satisfies a check with no implementation.** Every
rung therefore fails identically, the ladder is guaranteed to exhaust, and the
climb reaches metered tiers and is billed for attempts that could never have
passed. Measured on the dev lane, correlation
`6ce51f77-62c4-4785-93f5-42e06e6a0a67` at 2026-09-21T11:42Z: three local rungs
refused identically on the unsupported check, then a metered rung. The earlier
`73aba966-970c-4f29-987e-d85246152b2d` burned five rungs across two metered
tiers the same way.

This is the failure class the lab-pass wiring note already names: a check that
cannot pass is worse than one that cannot fail, because it stops the work.

NOT A REGRESSION. Before OMN-18978 caller criteria were concatenated into the
deterministic band unconditionally, so this fired whenever a caller named a
heuristic-only check, override or not. The union made it strictly rarer and did
not close it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from omnimarket.models.delegation.wire.model_quality_gate import (
    ModelQualityGateInput,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers import (
    handler_quality_gate as gate,
)

pytestmark = pytest.mark.unit

_UNSUPPORTED_PREFIX = "MALFORMED: unsupported deterministic DoD check"

#: The `test` class's own deterministic band, unaffected by a shape override.
_CLASS_DETERMINISTIC = (
    "compiles_without_errors",
    "final_artifact_only",
    "uses_pytest_mark_unit",
)


def _result(
    *,
    heuristic: tuple[str, ...],
    criteria: tuple[str, ...],
) -> object:
    """The gate input the measured run produced.

    `heuristic=()` is the shape-override case: the directive replaced the band,
    so the class's own heuristic rules are not declared for this request.
    """
    return gate.delta(
        ModelQualityGateInput(
            correlation_id="6ce51f77-62c4-4785-93f5-42e06e6a0a67",
            task_type="test",
            llm_response_content="Validation",
            dod_deterministic=_CLASS_DETERMINISTIC,
            dod_heuristic=heuristic,
            acceptance_criteria=criteria,
        )
    )


def _reasons(result: object) -> list[str]:
    return list(result.failure_reasons)  # type: ignore[attr-defined]


def _rules(result: object) -> dict[str, str]:
    return {
        evaluation.rule: str(evaluation.enforcement.value)  # type: ignore[attr-defined]
        for evaluation in result.rule_evaluations  # type: ignore[attr-defined]
    }


class TestTheBarIsAlwaysReachable:
    """AC1. The measured reproduction, and the general property behind it."""

    def test_the_measured_run_no_longer_produces_an_unrunnable_check(self) -> None:
        reasons = _reasons(_result(heuristic=(), criteria=("covers_edge_cases",)))
        unsupported = [r for r in reasons if r.startswith(_UNSUPPORTED_PREFIX)]
        assert not unsupported, (
            "the criterion landed in a band that cannot run it, so no answer "
            f"could have passed: {unsupported}"
        )

    def test_the_criterion_is_actually_evaluated(self) -> None:
        """Not-unrunnable is not enough; it has to be graded.

        A fix that silently DROPPED the criterion would satisfy the assertion
        above and quietly stop honouring `--criteria`, which is worse than the
        defect because it fails open.
        """
        assert "covers_edge_cases" in _rules(
            _result(heuristic=(), criteria=("covers_edge_cases",))
        )

    @pytest.mark.parametrize(
        "criteria",
        [
            pytest.param(("covers_edge_cases",), id="one heuristic-only name"),
            pytest.param(
                ("covers_edge_cases", "no_refusal"),
                id="the exact pair from the measured run",
            ),
            pytest.param(
                ("covers_error_paths", "covers_edge_cases"),
                id="two heuristic-only names",
            ),
        ],
    )
    def test_no_criterion_shape_yields_an_unrunnable_check(
        self, criteria: tuple[str, ...]
    ) -> None:
        reasons = _reasons(_result(heuristic=(), criteria=criteria))
        assert not [r for r in reasons if r.startswith(_UNSUPPORTED_PREFIX)]


class TestWhatMustNotChange:
    """Positive controls. Each of these would pass if the fix over-reached."""

    def test_a_name_with_a_deterministic_arm_stays_deterministic(self) -> None:
        """`no_refusal` has arms in BOTH bands, and blocking must win.

        Demoting it would turn a reject-only veto into a scored nudge and
        weaken the bar, which is the opposite of the intended change.
        """
        rules = _rules(_result(heuristic=(), criteria=("no_refusal",)))
        assert rules["no_refusal"] == "blocking"

    def test_a_genuinely_unknown_name_is_still_reported(self) -> None:
        """A typo or a retired check must still surface, loudly.

        The fix places a criterion against what can grade it; it does not
        swallow names nothing can grade. Losing that would hide real contract
        defects, which is how an unrunnable check reached a live lane.
        """
        reasons = _reasons(_result(heuristic=(), criteria=("covers_edge_casez",)))
        assert any(r.startswith(_UNSUPPORTED_PREFIX) for r in reasons)

    def test_the_declared_band_case_is_untouched(self) -> None:
        """With no shape override the class declares the rule and nothing moves."""
        rules = _rules(
            _result(
                heuristic=("no_refusal", "covers_edge_cases", "covers_error_paths"),
                criteria=("covers_edge_cases",),
            )
        )
        for rule in _CLASS_DETERMINISTIC:
            assert rule in rules
        assert "covers_edge_cases" in rules


class TestTheRegistryCannotDriftFromTheChain:
    """A frozenset beside an if/elif chain is drift-shaped, so pin it.

    `SUPPORTED_DETERMINISTIC_CHECKS` decides where a criterion is placed. If a
    check is added to the chain and not to the set, a criterion naming it is
    demoted to heuristic and silently stops blocking. If a name is in the set
    and not in the chain, a criterion naming it becomes unrunnable again --
    exactly this defect. Neither is visible by reading either one alone.
    """

    def _chain_names(self) -> set[str]:
        # The whole module, not one function: the chain has moved between
        # helpers before, and a reader scoped to a single function reports an
        # empty set, which this class then has to treat as a failure rather
        # than as parity.
        tree = ast.parse(Path(gate.__file__).read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare) or len(node.comparators) != 1:
                continue
            left, right = node.left, node.comparators[0]
            if (
                isinstance(left, ast.Name)
                and left.id == "check"
                and isinstance(node.ops[0], ast.Eq)
                and isinstance(right, ast.Constant)
                and isinstance(right.value, str)
            ):
                names.add(right.value)
        assert names, "read no check names out of the chain; repoint this test"
        return names

    def test_the_set_is_exactly_what_the_chain_executes(self) -> None:
        chain = self._chain_names()
        declared = set(gate.SUPPORTED_DETERMINISTIC_CHECKS)
        assert declared == chain, (
            "SUPPORTED_DETERMINISTIC_CHECKS and the deterministic dispatch "
            f"chain disagree. only in the set: {sorted(declared - chain)}; "
            f"only in the chain: {sorted(chain - declared)}"
        )
