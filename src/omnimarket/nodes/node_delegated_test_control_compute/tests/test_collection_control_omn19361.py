# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Executable AC2 probe: collection/setup controls remain accepted-weak."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_delegated_test_control_compute.handlers.handler_delegated_test_control import (
    HandlerDelegatedTestControl,
)
from omnimarket.nodes.node_delegated_test_control_compute.models.model_delegated_test_control import (
    EnumControlStatus,
    ModelControlGradeRequest,
    RunOutcome,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("prefix", ["failed_collection", "error_setup"])
@pytest.mark.parametrize(
    "mutation", [None, "passed", "no_tests", "failed_collection", "error_setup"]
)
def test_collection_control_without_call_failure_is_accepted_weak(
    prefix: RunOutcome, mutation: RunOutcome | None
) -> None:
    grade = HandlerDelegatedTestControl().handle(
        ModelControlGradeRequest(
            fixed_outcome="passed",
            prefix_outcome=prefix,
            mutation_outcome=mutation,
            mutation_requested=mutation is not None,
            prefix_ref_equals_fixed_ref=False,
        )
    )

    assert grade.status is EnumControlStatus.ACCEPTED_COLLECTION
    assert grade.headline is False


@pytest.mark.parametrize("prefix", ["failed_collection", "error_setup"])
def test_call_failure_on_mutation_is_headline_grade(prefix: RunOutcome) -> None:
    grade = HandlerDelegatedTestControl().handle(
        ModelControlGradeRequest(
            fixed_outcome="passed",
            prefix_outcome=prefix,
            mutation_outcome="failed_call",
            mutation_requested=True,
            prefix_ref_equals_fixed_ref=False,
        )
    )

    assert grade.status is EnumControlStatus.ACCEPTED_MUTATION
    assert grade.headline is True
