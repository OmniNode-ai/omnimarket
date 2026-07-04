# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain test for node_pr_delegated_fix_effect (WS-D/D2, OMN-13940).

Command -> worktree resolution -> ruff fix -> defense-in-depth re-check ->
commit-with-trailer -> pr_polish re-entry -> ModelDelegatedFixResult, using
injected fakes (zero real git/ruff/subprocess I/O). Also asserts the
contract's declared terminal_event topic (state-coverage-gate, OMN-13781).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.events.pr_delegated_fix import (
    EnumDelegatedFixOutcome,
    ModelDelegatedFixCommand,
)
from omnimarket.nodes.node_pr_delegated_fix_effect import (
    NodePrDelegatedFixEffect,
)
from omnimarket.nodes.node_pr_delegated_fix_effect.handlers.handler_delegated_fix import (
    HandlerDelegatedFix,
    PrPolishRunOutcome,
)

_NODE_DIR = (
    Path(__file__).parent.parent
    / "src"
    / "omnimarket"
    / "nodes"
    / ("node_pr_delegated_fix_effect")
)


class _FakeWorktreeResolver:
    def __init__(self, path: Path) -> None:
        self._path = path

    def resolve(self, **kwargs: object) -> Path:
        return self._path


class _FakeRuffRunner:
    def run(self, worktree: Path) -> None:
        return None


class _FakeGitDiffAdapter:
    def changed_files(self, worktree: Path) -> list[str]:
        return ["src/foo.py"]

    def diff_line_count(self, worktree: Path) -> int:
        return 6

    def commit_all(self, worktree: Path, message: str) -> str:
        return "deadbeef"

    def discard_changes(self, worktree: Path) -> None:
        raise AssertionError("accepted path must not discard changes")


class _FakePrPolishRunner:
    def run(self, **kwargs: object) -> PrPolishRunOutcome:
        return PrPolishRunOutcome(final_phase="done", error_message=None)


def _make_worktree(tmp_path: Path) -> Path:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: /tmp/somewhere\n")
    return worktree


@pytest.mark.unit
class TestPrDelegatedFixEffectGoldenChain:
    async def test_full_cycle_accepted(self, tmp_path: Path) -> None:
        worktree = _make_worktree(tmp_path)
        handler = HandlerDelegatedFix(
            worktree_resolver=_FakeWorktreeResolver(worktree),
            ruff_runner=_FakeRuffRunner(),
            git_diff_adapter=_FakeGitDiffAdapter(),
            pr_polish_runner=_FakePrPolishRunner(),
        )
        command = ModelDelegatedFixCommand(
            correlation_id=uuid4(),
            repo="OmniNode-ai/omnimarket",
            pr_number=777,
            ticket_id="OMN-13940",
            block_reason="code_failure",
            changed_files=["src/foo.py"],
            diff_total_lines=6,
            worktree_path=str(worktree),
            requested_at=datetime.now(tz=UTC),
        )

        result = await handler.handle(command)

        assert result.outcome == EnumDelegatedFixOutcome.ACCEPTED
        assert result.commit_sha == "deadbeef"
        assert result.cost_usd == 0.0
        assert result.detail

    def test_entry_point_wrapper_resolves(self) -> None:
        """NodePrDelegatedFixEffect is the ONEX entry-point wrapper class."""
        assert issubclass(NodePrDelegatedFixEffect, HandlerDelegatedFix)

    def test_terminal_event_topic_declared_in_contract(self) -> None:
        contract_text = (_NODE_DIR / "contract.yaml").read_text()
        assert "onex.evt.omnimarket.pr-delegated-fix-completed.v1" in contract_text
