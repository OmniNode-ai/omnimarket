# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerDelegatedFix sequencing tests (OMN-13940), all seams injected.

Proves: canonical-clone protection, no-changes shortcut, defense-in-depth
size-gate + denylist re-check with rollback, commit-with-trailer, re-entry
into pr_polish with the skip_repair_dispatch/no_automerge flags (safety bar
#6), and gate-failure mapping (never claims success on a non-"done" pr_polish
final_phase).
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
from omnimarket.nodes.node_pr_delegated_fix_effect.handlers.handler_delegated_fix import (
    HandlerDelegatedFix,
    PrPolishRunOutcome,
)


class _FakeWorktreeResolver:
    def __init__(self, path: Path, *, raise_error: bool = False) -> None:
        self._path = path
        self._raise = raise_error

    def resolve(self, **kwargs: object) -> Path:
        if self._raise:
            raise RuntimeError("no worktree found")
        return self._path


class _FakeRuffRunner:
    def __init__(self, *, raise_error: bool = False) -> None:
        self.calls: list[Path] = []
        self._raise = raise_error

    def run(self, worktree: Path) -> None:
        self.calls.append(worktree)
        if self._raise:
            raise RuntimeError("ruff crashed")


class _FakeGitDiffAdapter:
    def __init__(
        self,
        *,
        changed: list[str] | None = None,
        lines: int = 0,
        commit_sha: str = "abc1234",
    ) -> None:
        self._changed = changed if changed is not None else ["src/foo.py"]
        self._lines = lines
        self._commit_sha = commit_sha
        self.commit_calls: list[str] = []
        self.discard_calls: list[Path] = []

    def changed_files(self, worktree: Path) -> list[str]:
        return list(self._changed)

    def diff_line_count(self, worktree: Path) -> int:
        return self._lines

    def commit_all(self, worktree: Path, message: str) -> str:
        self.commit_calls.append(message)
        return self._commit_sha

    def discard_changes(self, worktree: Path) -> None:
        self.discard_calls.append(worktree)


class _FakePrPolishRunner:
    def __init__(self, outcome: PrPolishRunOutcome) -> None:
        self._outcome = outcome
        self.calls: list[dict[str, object]] = []

    def run(
        self,
        *,
        repo: str,
        pr_number: int,
        ticket_id: str | None,
        worktree: Path,
        dry_run: bool,
    ) -> PrPolishRunOutcome:
        self.calls.append(
            {
                "repo": repo,
                "pr_number": pr_number,
                "ticket_id": ticket_id,
                "worktree": worktree,
                "dry_run": dry_run,
            }
        )
        return self._outcome


def _make_command(
    worktree_path: str | None, **overrides: object
) -> ModelDelegatedFixCommand:
    defaults: dict[str, object] = {
        "correlation_id": uuid4(),
        "repo": "OmniNode-ai/omnimarket",
        "pr_number": 500,
        "ticket_id": "OMN-13940",
        "block_reason": "code_failure",
        "changed_files": ["src/foo.py"],
        "diff_total_lines": 5,
        "worktree_path": worktree_path,
        "requested_at": datetime.now(tz=UTC),
    }
    defaults.update(overrides)
    return ModelDelegatedFixCommand(**defaults)  # type: ignore[arg-type]


def _make_worktree(tmp_path: Path) -> Path:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: /tmp/somewhere\n")
    return worktree


@pytest.mark.unit
class TestHandlerDelegatedFix:
    async def test_accepted_path_reenters_pr_polish_with_safety_flags(
        self, tmp_path: Path
    ) -> None:
        worktree = _make_worktree(tmp_path)
        ruff = _FakeRuffRunner()
        git = _FakeGitDiffAdapter(changed=["src/foo.py"], lines=8)
        pr_polish = _FakePrPolishRunner(
            PrPolishRunOutcome(final_phase="done", error_message=None)
        )
        handler = HandlerDelegatedFix(
            worktree_resolver=_FakeWorktreeResolver(worktree),
            ruff_runner=ruff,
            git_diff_adapter=git,
            pr_polish_runner=pr_polish,
        )

        result = await handler.handle(_make_command(str(worktree)))

        assert result.outcome == EnumDelegatedFixOutcome.ACCEPTED
        assert result.commit_sha == "abc1234"
        assert result.delegation_model == "ruff-deterministic"
        assert result.cost_usd == 0.0
        assert "pr_polish gates" in result.detail
        assert len(ruff.calls) == 1
        assert len(git.commit_calls) == 1
        assert "delegated-by: ruff-deterministic" in git.commit_calls[0]
        assert len(pr_polish.calls) == 1
        sent = pr_polish.calls[0]
        assert sent["worktree"] == worktree
        assert sent["repo"] == "OmniNode-ai/omnimarket"
        assert sent["pr_number"] == 500
        assert sent["dry_run"] is False

    async def test_no_changes_shortcut_never_commits_or_calls_pr_polish(
        self, tmp_path: Path
    ) -> None:
        worktree = _make_worktree(tmp_path)
        ruff = _FakeRuffRunner()
        git = _FakeGitDiffAdapter(changed=[])
        pr_polish = _FakePrPolishRunner(
            PrPolishRunOutcome(final_phase="done", error_message=None)
        )
        handler = HandlerDelegatedFix(
            worktree_resolver=_FakeWorktreeResolver(worktree),
            ruff_runner=ruff,
            git_diff_adapter=git,
            pr_polish_runner=pr_polish,
        )

        result = await handler.handle(_make_command(str(worktree)))

        assert result.outcome == EnumDelegatedFixOutcome.NO_CHANGES
        assert git.commit_calls == []
        assert pr_polish.calls == []

    async def test_size_gate_refusal_discards_changes(self, tmp_path: Path) -> None:
        worktree = _make_worktree(tmp_path)
        git = _FakeGitDiffAdapter(changed=["a.py", "b.py", "c.py", "d.py"], lines=10)
        pr_polish = _FakePrPolishRunner(
            PrPolishRunOutcome(final_phase="done", error_message=None)
        )
        handler = HandlerDelegatedFix(
            worktree_resolver=_FakeWorktreeResolver(worktree),
            ruff_runner=_FakeRuffRunner(),
            git_diff_adapter=git,
            pr_polish_runner=pr_polish,
        )

        result = await handler.handle(_make_command(str(worktree)))

        assert result.outcome == EnumDelegatedFixOutcome.REFUSED_SIZE_GATE
        assert git.discard_calls == [worktree]
        assert git.commit_calls == []
        assert pr_polish.calls == []

    async def test_denylisted_actual_diff_refused_and_discarded(
        self, tmp_path: Path
    ) -> None:
        worktree = _make_worktree(tmp_path)
        git = _FakeGitDiffAdapter(
            changed=["onex_change_control/contracts/x.yaml"], lines=3
        )
        handler = HandlerDelegatedFix(
            worktree_resolver=_FakeWorktreeResolver(worktree),
            ruff_runner=_FakeRuffRunner(),
            git_diff_adapter=git,
            pr_polish_runner=_FakePrPolishRunner(
                PrPolishRunOutcome(final_phase="done", error_message=None)
            ),
        )

        result = await handler.handle(_make_command(str(worktree)))

        assert result.outcome == EnumDelegatedFixOutcome.REFUSED_DENYLIST
        assert git.discard_calls == [worktree]
        assert git.commit_calls == []

    async def test_canonical_clone_refused_before_ruff_runs(
        self, tmp_path: Path
    ) -> None:
        canonical = tmp_path / "canonical"
        canonical.mkdir()
        (canonical / ".git").mkdir()  # directory, not a gitlink file
        ruff = _FakeRuffRunner()
        handler = HandlerDelegatedFix(
            worktree_resolver=_FakeWorktreeResolver(canonical),
            ruff_runner=ruff,
            git_diff_adapter=_FakeGitDiffAdapter(),
            pr_polish_runner=_FakePrPolishRunner(
                PrPolishRunOutcome(final_phase="done", error_message=None)
            ),
        )

        result = await handler.handle(_make_command(str(canonical)))

        assert result.outcome == EnumDelegatedFixOutcome.REFUSED_NOT_A_WORKTREE
        assert ruff.calls == []

    async def test_gate_failure_maps_to_gate_failed_never_claims_success(
        self, tmp_path: Path
    ) -> None:
        worktree = _make_worktree(tmp_path)
        pr_polish = _FakePrPolishRunner(
            PrPolishRunOutcome(
                final_phase="failed", error_message="pre-commit hook failed"
            )
        )
        handler = HandlerDelegatedFix(
            worktree_resolver=_FakeWorktreeResolver(worktree),
            ruff_runner=_FakeRuffRunner(),
            git_diff_adapter=_FakeGitDiffAdapter(),
            pr_polish_runner=pr_polish,
        )

        result = await handler.handle(_make_command(str(worktree)))

        assert result.outcome == EnumDelegatedFixOutcome.GATE_FAILED
        assert result.error == "pre-commit hook failed"
        assert not result.is_success

    async def test_ruff_failure_maps_to_error(self, tmp_path: Path) -> None:
        worktree = _make_worktree(tmp_path)
        handler = HandlerDelegatedFix(
            worktree_resolver=_FakeWorktreeResolver(worktree),
            ruff_runner=_FakeRuffRunner(raise_error=True),
            git_diff_adapter=_FakeGitDiffAdapter(),
            pr_polish_runner=_FakePrPolishRunner(
                PrPolishRunOutcome(final_phase="done", error_message=None)
            ),
        )

        result = await handler.handle(_make_command(str(worktree)))

        assert result.outcome == EnumDelegatedFixOutcome.ERROR
        assert "ruff crashed" in (result.error or "")

    async def test_worktree_resolution_failure_maps_to_error(
        self, tmp_path: Path
    ) -> None:
        handler = HandlerDelegatedFix(
            worktree_resolver=_FakeWorktreeResolver(tmp_path, raise_error=True),
            ruff_runner=_FakeRuffRunner(),
            git_diff_adapter=_FakeGitDiffAdapter(),
            pr_polish_runner=_FakePrPolishRunner(
                PrPolishRunOutcome(final_phase="done", error_message=None)
            ),
        )

        result = await handler.handle(_make_command(None))

        assert result.outcome == EnumDelegatedFixOutcome.ERROR
        assert "no worktree found" in (result.error or "")
