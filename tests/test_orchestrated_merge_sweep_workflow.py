from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

import scripts.run_orchestrated_merge_sweep_workflow as workflow
from omnimarket.nodes.node_pr_polish.models.model_pr_polish_completed_event import (
    ModelPrPolishCompletedEvent,
)
from omnimarket.nodes.node_pr_polish.models.model_pr_polish_start_command import (
    ModelPrPolishStartCommand,
)
from omnimarket.nodes.node_pr_polish.models.model_pr_polish_state import (
    EnumPrPolishPhase,
)
from omnimarket.nodes.node_sweep_outcome_classify.models.model_sweep_outcome import (
    EnumSweepOutcome,
)

_CORR_ID = UUID("00000000-0000-4000-a000-000000000002")
_RUN_ID = UUID("00000000-0000-4000-a000-000000000003")


def test_prepare_polish_worktree_uses_clean_current_pr_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        workflow,
        "_pr_head",
        lambda _repo, _pr_number: ("jonah/omn-10400-test", "abc123"),
    )
    monkeypatch.setattr(workflow, "_worktree_head_sha", lambda _worktree: "abc123")
    monkeypatch.setattr(workflow, "_is_clean_worktree", lambda _worktree: True)

    worktree, created_by_script, created_branch = workflow._prepare_polish_worktree(
        "OmniNode-ai/omnimarket", 465
    )

    assert worktree == workflow.REPO_ROOT
    assert created_by_script is False
    assert created_branch is None


def test_prepare_polish_worktree_rejects_dirty_current_pr_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        workflow,
        "_pr_head",
        lambda _repo, _pr_number: ("jonah/omn-10400-test", "abc123"),
    )
    monkeypatch.setattr(workflow, "_worktree_head_sha", lambda _worktree: "abc123")
    monkeypatch.setattr(workflow, "_is_clean_worktree", lambda _worktree: False)

    with pytest.raises(RuntimeError, match="current PR-head worktree is dirty"):
        workflow._prepare_polish_worktree("OmniNode-ai/omnimarket", 465)


def test_prepare_polish_worktree_ignores_dirty_branch_candidate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dirty = tmp_path / "dirty"
    dirty.mkdir()
    calls: list[list[str]] = []

    monkeypatch.setattr(
        workflow,
        "_pr_head",
        lambda _repo, _pr_number: ("jonah/omn-10400-test", "abc123"),
    )
    monkeypatch.setattr(
        workflow, "_worktree_head_sha", lambda _worktree: "different-sha"
    )
    monkeypatch.setattr(workflow, "_worktrees_for_branch", lambda _branch: [dirty])
    monkeypatch.setattr(workflow, "_is_clean_worktree", lambda _worktree: False)
    monkeypatch.setattr(
        workflow.tempfile, "mkdtemp", lambda **_kwargs: str(tmp_path / "tmp")
    )

    def fake_run(argv: list[str], **kwargs: object) -> str:
        calls.append(argv)
        return ""

    monkeypatch.setattr(workflow, "_run", fake_run)

    worktree, created_by_script, created_branch = workflow._prepare_polish_worktree(
        "OmniNode-ai/omnimarket", 465
    )

    assert worktree == tmp_path / "tmp" / "worktree"
    assert created_by_script is True
    assert created_branch is not None
    assert calls[0][:3] == ["git", "fetch", "origin"]
    assert calls[1][:3] == ["git", "worktree", "add"]


@pytest.mark.asyncio
async def test_pr_polish_completion_is_returned_for_reducer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    command = ModelPrPolishStartCommand(
        correlation_id=_CORR_ID,
        repo="OmniNode-ai/omnimarket",
        pr_number=465,
        requested_at=datetime.now(UTC),
    )
    completed = ModelPrPolishCompletedEvent(
        correlation_id=_CORR_ID,
        final_phase=EnumPrPolishPhase.DONE,
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
        pr_number=465,
    )
    monkeypatch.setattr(
        workflow,
        "_prepare_polish_worktree",
        lambda _repo, _pr_number: (tmp_path, False, None),
    )
    monkeypatch.setattr(workflow, "run_live_pr_polish", lambda _command: completed)
    monkeypatch.setattr(
        workflow, "_cleanup_polish_worktree", lambda *_args, **_kwargs: None
    )

    outcomes = await workflow._execute_command(
        command,
        execute=True,
        run_id=_RUN_ID,
        state_dir=tmp_path,
        total_prs=1,
        keep_worktrees=False,
    )

    assert len(outcomes) == 1
    assert outcomes[0].repo == "OmniNode-ai/omnimarket"
    assert outcomes[0].pr_number == 465
    assert outcomes[0].outcome == EnumSweepOutcome.SUCCESS
