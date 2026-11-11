# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam with the trial driver's run request.

The driver's model is not importable on this base, so its side is the pinned
constants below. Lane eval-e1b-4830, pull request 5861, head
456225c74db811208ce2f3b44f24b3f189a2310b.
"""

from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from omnimarket.models.code_eval_suite.model_suite_census_result import (
    ModelCodeEvalSuiteCandidate,
)
from omnimarket.nodes.node_code_contract_score_effect.models import (
    EnumCodeScoreVerdict,
)
from tests.nodes.node_code_eval_suite_census_compute.support import task

pytestmark = pytest.mark.unit

FIELD_SET = frozenset({"repository", "merge_commit", "contract_id"})
MERGE_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
CONTRACT_ID_PATTERN = re.compile(r"^OMN-[0-9]+$")


def test_model_fields_keys_equal_the_pinned_field_set() -> None:
    assert set(ModelCodeEvalSuiteCandidate.model_fields) == FIELD_SET


def test_candidate_dumps_to_a_dict_with_the_pinned_keys_and_patterns() -> None:
    t = task(
        repository="omnimarket",
        merge_commit="a" * 40,
        contract_id="OMN-123",
        control=EnumCodeScoreVerdict.PASS,
    )
    candidate = ModelCodeEvalSuiteCandidate(
        repository=t.repository,
        merge_commit=t.merge_commit,
        contract_id=t.contract_id,
    )
    dumped = candidate.model_dump()
    assert set(dumped) == FIELD_SET
    assert MERGE_COMMIT_PATTERN.match(dumped["merge_commit"])
    assert CONTRACT_ID_PATTERN.match(dumped["contract_id"])


def test_unknown_key_on_a_candidate_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelCodeEvalSuiteCandidate(
            repository="omnimarket",
            merge_commit="a" * 40,
            contract_id="OMN-123",
            unknown="x",  # type: ignore[call-arg]
        )
