# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerDelegatedTestControl — grade a must-fail control (OMN-19361).

Pure definition-B compute: ``handle(request: ModelControlGradeRequest) ->
ModelControlGrade``. No I/O, no envelope type.

A port of the delegation ladder's discrimination rule
(``benchmarks/delegation_ladder/scorers.py``, ``score_unit_test_execution``):
a generated test is credited only when it passes the real code AND fails the
broken code. A test that passes both does not test anything. Here "the broken
code" is the pre-fix commit, or, when the pre-fix code lacks the symbol the
test imports, a mutation of the fixed commit (operator ruling
2026-09-23T21:52:08Z decision (3)).

The decision, in the order it is applied:

1. The negative control (pre-fix ref == fixed ref) never accepts. A pass is
   ``control_did_not_fail``; anything else means the same ref disagreed with
   itself, which is ``infra_error``.
2. An infrastructure fault at the control is ``infra_error``.
3. A pass (or no test) at the pre-fix ref is ``control_did_not_fail``.
4. A call-phase failure at the pre-fix ref is ``accepted_call`` (headline).
5. A collection or setup failure at the pre-fix ref is accepted-weak. When the
   request carries a mutation, the mutation decides the headline: not yet run
   is ``needs_mutation_control``; a call-phase failure there is
   ``accepted_mutation`` (headline); an infrastructure fault is
   ``infra_error``; anything else stays ``accepted_collection``.
"""

from __future__ import annotations

from omnimarket.nodes.node_delegated_test_control_compute.models.model_delegated_test_control import (
    HEADLINE_STATUSES,
    EnumControlStatus,
    ModelControlGrade,
    ModelControlGradeRequest,
)

_WEAK_PREFIX_FAILURES = frozenset({"failed_collection", "error_setup"})


def _grade(
    status: EnumControlStatus, role: str, outcome: str, reason: str
) -> ModelControlGrade:
    return ModelControlGrade(
        status=status,
        headline=status in HEADLINE_STATUSES,
        control_ref_role="mutation" if role == "mutation" else "prefix",
        control_outcome=outcome,
        reason=reason,
    )


def grade_control(request: ModelControlGradeRequest) -> ModelControlGrade:
    prefix = request.prefix_outcome
    if request.prefix_ref_equals_fixed_ref:
        if prefix == "passed":
            return _grade(
                EnumControlStatus.CONTROL_DID_NOT_FAIL,
                "prefix",
                prefix,
                "negative control: the pre-fix ref is the fixed ref and the test passed",
            )
        return _grade(
            EnumControlStatus.INFRA_ERROR,
            "prefix",
            prefix,
            "negative control: the same ref passed and then did not (flaky or broken run)",
        )
    if prefix == "infra_error":
        return _grade(
            EnumControlStatus.INFRA_ERROR, "prefix", prefix, "the control run faulted"
        )
    if prefix in ("passed", "no_tests"):
        return _grade(
            EnumControlStatus.CONTROL_DID_NOT_FAIL,
            "prefix",
            prefix,
            "the test did not fail at the pre-fix ref, so it does not discriminate",
        )
    if prefix == "failed_call":
        return _grade(
            EnumControlStatus.ACCEPTED_CALL,
            "prefix",
            prefix,
            "the test's assertion failed at the pre-fix ref",
        )
    if prefix not in _WEAK_PREFIX_FAILURES:  # pragma: no cover - Literal-closed
        return _grade(
            EnumControlStatus.INFRA_ERROR, "prefix", prefix, "unknown outcome"
        )

    if not request.mutation_requested:
        return _grade(
            EnumControlStatus.ACCEPTED_COLLECTION,
            "prefix",
            prefix,
            "the pre-fix ref failed only at collection or setup, and no mutation was given",
        )
    mutation = request.mutation_outcome
    if mutation is None:
        return _grade(
            EnumControlStatus.NEEDS_MUTATION_CONTROL,
            "prefix",
            prefix,
            "the pre-fix ref failed only at collection or setup; run the mutation control",
        )
    if mutation == "infra_error":
        return _grade(
            EnumControlStatus.INFRA_ERROR,
            "mutation",
            mutation,
            "the mutation run faulted",
        )
    if mutation == "failed_call":
        return _grade(
            EnumControlStatus.ACCEPTED_MUTATION,
            "mutation",
            mutation,
            "the test's assertion failed against the mutated fixed commit",
        )
    return _grade(
        EnumControlStatus.ACCEPTED_COLLECTION,
        "mutation",
        mutation,
        "the mutated fixed commit did not fail the test at assertion level",
    )


class HandlerDelegatedTestControl:
    """COMPUTE handler: the control outcomes in, one status out."""

    def handle(self, request: ModelControlGradeRequest) -> ModelControlGrade:
        return grade_control(request)


__all__ = ["HandlerDelegatedTestControl", "grade_control"]
