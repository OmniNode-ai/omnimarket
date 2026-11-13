# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2 of OMN-23626: failing controls. Each injected break turns its reading red; the unbroken handler reads green.

The breaks replace one rule of the handler through the two methods that hold it: ``_place_checks`` (skip placing the
check files) and ``_scoring_commit`` (run the check at the merge commit instead of the base plus the draft).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.models.model_delegated_test_control import (
    EnumControlStatus,
    ModelControlGrade,
)
from omnimarket.nodes.node_code_contract_score_effect.handlers import (
    handler_code_contract_score,
)
from omnimarket.nodes.node_code_contract_score_effect.handlers.handler_code_contract_score import (
    HandlerCodeContractScore,
)
from omnimarket.nodes.node_code_contract_score_effect.models import (
    EnumCodeScoreSource,
    EnumCodeScoreVerdict,
    ModelCodeScoreRequest,
)
from tests.nodes.node_code_contract_score_effect.fixture_repo import Fixture, request

pytestmark = pytest.mark.unit


class SkipPlacingCheckFiles(HandlerCodeContractScore):
    """Break 1: the check files are never placed over the worktree."""

    def _place_checks(
        self,
        request: ModelCodeScoreRequest,
        files: dict[str, bytes],
        worktree: Path,
    ) -> None:
        return None


class RunAtMergeCommit(HandlerCodeContractScore):
    """Break 2: the draft is judged at the merge commit, not at the base plus the draft."""

    merge: str = ""

    def _scoring_commit(self, request: ModelCodeScoreRequest) -> str:
        return self.merge


async def known_good_scores_pass(
    handler: HandlerCodeContractScore, fx: Fixture
) -> None:
    result = await handler.handle(request(fx, fx.good_diff))
    assert result.scored is not None
    assert result.scored.verdict.checks is EnumCodeScoreVerdict.PASS


async def empty_draft_scores_fail_check(
    handler: HandlerCodeContractScore, fx: Fixture
) -> None:
    result = await handler.handle(request(fx, ""))
    assert result.scored is not None
    assert result.scored.verdict.checks is EnumCodeScoreVerdict.FAIL_CHECK


async def test_control_unbroken_handler_reads_green(fx: Fixture) -> None:
    await known_good_scores_pass(HandlerCodeContractScore(), fx)
    await empty_draft_scores_fail_check(HandlerCodeContractScore(), fx)


async def test_break_skip_placing_check_files_turns_the_pass_red(fx: Fixture) -> None:
    broken = SkipPlacingCheckFiles()
    result = await broken.handle(request(fx, fx.good_diff))
    assert result.scored is not None
    assert result.scored.verdict.checks is not EnumCodeScoreVerdict.PASS
    with pytest.raises(AssertionError):
        await known_good_scores_pass(broken, fx)


async def test_break_run_at_merge_commit_makes_the_empty_draft_pass(
    fx: Fixture,
) -> None:
    broken = RunAtMergeCommit()
    broken.merge = fx.merge
    result = await broken.handle(request(fx, ""))
    assert result.scored is not None
    assert result.scored.verdict.checks is EnumCodeScoreVerdict.PASS
    with pytest.raises(AssertionError):
        await empty_draft_scores_fail_check(broken, fx)


async def test_the_must_fail_control_is_graded_by_the_control_compute(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The falsifier decision is node_delegated_test_control_compute's: its grade, not this handler, decides."""
    grade = ModelControlGrade(
        status=EnumControlStatus.CONTROL_DID_NOT_FAIL,
        headline=False,
        control_ref_role="prefix",
        control_outcome="passed",
        reason="injected grade",
    )
    monkeypatch.setattr(
        handler_code_contract_score, "grade_control", lambda _request: grade
    )
    result = await HandlerCodeContractScore().handle(request(fx, fx.good_diff))
    assert result.scored is not None
    verdict = result.scored.verdict
    assert verdict.checks is EnumCodeScoreVerdict.UNSCORABLE
    assert verdict.source is EnumCodeScoreSource.FALSIFIER_NOT_HELD
    assert "injected grade" in verdict.detail
