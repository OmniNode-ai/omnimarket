# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam with node_code_contract_score_effect.

ModelCodeEvalSuiteTask.control is typed with the scorer's EnumCodeScoreVerdict
and control_source with its EnumCodeScoreSource; the member sets are pinned
below.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_code_contract_score_effect.models import (
    EnumCodeScoreSource,
    EnumCodeScoreVerdict,
)
from tests.nodes.node_code_eval_suite_census_compute.support import task

pytestmark = pytest.mark.unit

PINNED_VERDICTS = frozenset(
    {
        "pass",
        "fail_check",
        "fail_no_diff",
        "fail_did_not_apply",
        "fail_tampered",
        "unscorable",
    }
)
PINNED_SOURCES = frozenset(
    {
        "contract",
        "check_not_runnable",
        "falsifier_not_held",
        "env",
        "worktree",
        "harness",
        "context",
        "draft_not_recoverable",
    }
)


def test_enum_code_score_verdict_members_equal_the_pinned_set() -> None:
    assert {m.value for m in EnumCodeScoreVerdict} == PINNED_VERDICTS


def test_enum_code_score_source_members_equal_the_pinned_set() -> None:
    assert {m.value for m in EnumCodeScoreSource} == PINNED_SOURCES


def test_task_control_is_typed_with_the_scorer_enum() -> None:
    t = task(control=EnumCodeScoreVerdict.PASS)
    assert isinstance(t.control, EnumCodeScoreVerdict)
    assert t.control is EnumCodeScoreVerdict.PASS
