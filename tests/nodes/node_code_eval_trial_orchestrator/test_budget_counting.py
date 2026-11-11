# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23627 AC13 -- budget counting (rule 7): budget k reads the first k verdicts."""

from __future__ import annotations

import itertools

import pytest

from omnimarket.nodes.node_code_contract_score_effect.models.enum_code_score import (
    EnumCodeScoreVerdict as V,
)
from omnimarket.nodes.node_code_eval_trial_reducer.handlers import (
    budget_counts,
    budget_reading,
)

pytestmark = pytest.mark.unit

U, F, P = V.UNSCORABLE, V.FAIL_CHECK, V.PASS


def test_unscorable_then_two_fails_leaves_n_at_one_and_at_three() -> None:
    assert budget_reading([U, F, F], 1) is None
    assert budget_reading([U, F, F], 3) is None
    assert budget_counts([[U, F, F]], (1, 3)) == {1: (0, 0), 3: (0, 0)}


def test_fail_unscorable_pass_fails_at_one_and_passes_at_three() -> None:
    assert budget_reading([F, U, P], 1) is False
    assert budget_reading([F, U, P], 3) is True
    assert budget_counts([[F, U, P]], (1, 3)) == {1: (1, 0), 3: (1, 1)}


def test_fail_unscorable_fail_fails_at_one_and_leaves_n_at_three() -> None:
    assert budget_reading([F, U, F], 1) is False
    assert budget_reading([F, U, F], 3) is None
    assert budget_counts([[F, U, F]], (1, 3)) == {1: (1, 0), 3: (0, 0)}


def test_a_sequence_that_passed_early_passes_every_larger_budget() -> None:
    assert [budget_reading([P], k) for k in (1, 3, 5)] == [True, True, True]


def test_n_and_passes_equal_a_recount_of_kept_sequences() -> None:
    verdicts = (P, F, U)
    sequences = [list(s) for s in itertools.product(verdicts, repeat=3)]
    counts = budget_counts(sequences, (1, 3))
    for k in (1, 3):
        kept = [s for s in sequences if P in s[:k] or U not in s[:k]]
        passes = [s for s in kept if P in s[:k]]
        assert counts[k] == (len(kept), len(passes))
