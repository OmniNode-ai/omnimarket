# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2 of OMN-23626: failing controls. Each injected break turns its reading red; the unbroken handler reads green.

The breaks replace one rule of the handler through the two methods that hold it: ``_place_checks`` (skip placing the
check files) and ``_scoring_commit`` (run the check at the merge commit instead of the base plus the draft).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_code_contract_score_effect.handlers.handler_code_contract_score import (
    HandlerCodeContractScore,
)
from omnimarket.nodes.node_code_contract_score_effect.models import EnumCodeScoreVerdict
from tests.nodes.node_code_contract_score_effect.fixture_repo import Fixture, request

pytestmark = pytest.mark.unit


class SkipPlacingCheckFiles(HandlerCodeContractScore):
    """Break 1: the check files are never placed over the worktree."""

    def _place_checks(self, request, files, worktree: Path) -> None:  # type: ignore[no-untyped-def]
        return None


class RunAtMergeCommit(HandlerCodeContractScore):
    """Break 2: the draft is judged at the merge commit, not at the base plus the draft."""

    merge: str = ""

    def _scoring_commit(self, request) -> str:  # type: ignore[no-untyped-def]
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
