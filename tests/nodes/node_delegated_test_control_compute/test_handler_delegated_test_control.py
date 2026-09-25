# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19361 — the must-fail control grading compute.

The table below is the whole decision. The operator ruling of 2026-09-23
(decision 3) is what separates the two headline statuses from the weak one: a
pre-fix failure at collection or setup is accepted-weak, and only an
assertion-level failure (at the pre-fix ref, or at the mutated fixed ref when
the pre-fix code lacks the symbol) is headline-grade.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_delegated_test_control_compute.handlers.handler_delegated_test_control import (
    HandlerDelegatedTestControl,
    grade_control,
)
from omnimarket.nodes.node_delegated_test_control_compute.models.model_delegated_test_control import (
    HEADLINE_STATUSES,
    EnumControlStatus,
    ModelControlGradeRequest,
)

pytestmark = pytest.mark.unit

PASSED = "passed"
CALL = "failed_call"
COLLECTION = "failed_collection"
SETUP = "error_setup"
NO_TESTS = "no_tests"
INFRA = "infra_error"


def _grade(
    prefix: str,
    *,
    mutation: str | None = None,
    requested: bool = False,
    same_ref: bool = False,
) -> EnumControlStatus:
    request = ModelControlGradeRequest(
        fixed_outcome=PASSED,
        prefix_outcome=prefix,
        mutation_outcome=mutation,
        mutation_requested=requested,
        prefix_ref_equals_fixed_ref=same_ref,
    )
    return grade_control(request).status


@pytest.mark.parametrize(
    ("prefix", "mutation", "requested", "status"),
    [
        (PASSED, None, False, EnumControlStatus.CONTROL_DID_NOT_FAIL),
        (PASSED, None, True, EnumControlStatus.CONTROL_DID_NOT_FAIL),
        (NO_TESTS, None, False, EnumControlStatus.CONTROL_DID_NOT_FAIL),
        (CALL, None, False, EnumControlStatus.ACCEPTED_CALL),
        (CALL, None, True, EnumControlStatus.ACCEPTED_CALL),
        (COLLECTION, None, False, EnumControlStatus.ACCEPTED_COLLECTION),
        (SETUP, None, False, EnumControlStatus.ACCEPTED_COLLECTION),
        (COLLECTION, None, True, EnumControlStatus.NEEDS_MUTATION_CONTROL),
        (COLLECTION, CALL, True, EnumControlStatus.ACCEPTED_MUTATION),
        (COLLECTION, PASSED, True, EnumControlStatus.ACCEPTED_COLLECTION),
        (COLLECTION, COLLECTION, True, EnumControlStatus.ACCEPTED_COLLECTION),
        (COLLECTION, SETUP, True, EnumControlStatus.ACCEPTED_COLLECTION),
        (COLLECTION, INFRA, True, EnumControlStatus.INFRA_ERROR),
        (INFRA, None, False, EnumControlStatus.INFRA_ERROR),
    ],
)
def test_the_control_table(
    prefix: str, mutation: str | None, requested: bool, status: EnumControlStatus
) -> None:
    assert _grade(prefix, mutation=mutation, requested=requested) is status


def test_a_pass_at_both_refs_is_control_did_not_fail() -> None:
    assert _grade(PASSED) is EnumControlStatus.CONTROL_DID_NOT_FAIL
    assert not _grade(PASSED).value.startswith("accepted")


def test_a_collection_only_failure_is_never_accepted_call_or_mutation() -> None:
    status = _grade(COLLECTION, mutation=PASSED, requested=True)
    assert status is EnumControlStatus.ACCEPTED_COLLECTION
    assert status not in HEADLINE_STATUSES


def test_a_collection_failure_plus_a_call_failure_on_the_mutation_is_accepted_mutation() -> (
    None
):
    status = _grade(COLLECTION, mutation=CALL, requested=True)
    assert status is EnumControlStatus.ACCEPTED_MUTATION
    assert status in HEADLINE_STATUSES


@pytest.mark.parametrize("prefix", [PASSED, CALL, COLLECTION, SETUP, NO_TESTS, INFRA])
@pytest.mark.parametrize("mutation", [None, PASSED, CALL, COLLECTION, INFRA])
def test_prefix_equal_to_fixed_never_accepts(prefix: str, mutation: str | None) -> None:
    status = _grade(
        prefix, mutation=mutation, requested=mutation is not None, same_ref=True
    )
    assert not status.value.startswith("accepted")
    assert status in {
        EnumControlStatus.CONTROL_DID_NOT_FAIL,
        EnumControlStatus.INFRA_ERROR,
    }


def test_the_headline_set_is_exactly_the_two_assertion_level_statuses() -> None:
    assert (
        frozenset(
            {EnumControlStatus.ACCEPTED_CALL, EnumControlStatus.ACCEPTED_MUTATION}
        )
        == HEADLINE_STATUSES
    )


def test_grading_refuses_a_fixed_run_that_did_not_pass() -> None:
    with pytest.raises(ValueError, match="fixed ref"):
        ModelControlGradeRequest(
            fixed_outcome=CALL,
            prefix_outcome=CALL,
            mutation_requested=False,
            prefix_ref_equals_fixed_ref=False,
        )


def test_the_handler_is_the_same_pure_function() -> None:
    request = ModelControlGradeRequest(
        fixed_outcome=PASSED,
        prefix_outcome=CALL,
        mutation_requested=False,
        prefix_ref_equals_fixed_ref=False,
    )
    assert HandlerDelegatedTestControl().handle(request) == grade_control(request)


def test_the_grade_names_its_control_run_and_reason() -> None:
    grade = grade_control(
        ModelControlGradeRequest(
            fixed_outcome=PASSED,
            prefix_outcome=COLLECTION,
            mutation_outcome=CALL,
            mutation_requested=True,
            prefix_ref_equals_fixed_ref=False,
        )
    )
    assert grade.headline is True
    assert grade.control_ref_role == "mutation"
    assert grade.control_outcome == CALL
    assert "mutated" in grade.reason
