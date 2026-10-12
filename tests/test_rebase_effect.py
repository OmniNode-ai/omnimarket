# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Task 6: Tests for node_rebase_effect [OMN-8961]."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.nodes.node_merge_sweep_triage_orchestrator.models.model_triage_request import (
    ModelRebaseCommand,
)
from omnimarket.nodes.node_rebase_effect.handlers.handler_rebase import (
    HandlerRebaseEffect,
    _worktree_root,
)
from omnimarket.nodes.node_rebase_effect.models.model_rebase_completed_event import (
    ModelRebaseCompletedEvent,
)

_RUN_ID = UUID("00000000-0000-4000-a000-000000000001")
_CORR_ID = UUID("00000000-0000-4000-a000-000000000002")


def _cmd(
    head_ref: str = "feat/test",
    base_ref: str = "main",
    head_oid: str = "abc123",
) -> ModelRebaseCommand:
    return ModelRebaseCommand(
        pr_number=200,
        repo="OmniNode-ai/omni_home",
        head_ref_name=head_ref,
        base_ref_name=base_ref,
        head_ref_oid=head_oid,
        correlation_id=_CORR_ID,
        run_id=_RUN_ID,
        total_prs=3,
    )


def test_blank_worktree_root_uses_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEX_REBASE_WORKTREE_ROOT", "   ")
    assert _worktree_root() == Path.home() / ".cache" / "onex-rebase"


@pytest.mark.asyncio
async def test_protected_head_ref_refuses_without_git(tmp_path: Path) -> None:
    """Rule: refuse to rebase if head_ref is a protected branch (main/master/develop)."""
    handler = HandlerRebaseEffect()
    # No git calls should happen — guard fires before any subprocess
    evt = await handler.handle(
        ModelRebaseCommand(
            pr_number=300,
            repo="OmniNode-ai/omni_home",
            head_ref_name="main",  # protected!
            base_ref_name="main",
            head_ref_oid="abc",
            correlation_id=_CORR_ID,
            run_id=_RUN_ID,
            total_prs=1,
        )
    )
    assert isinstance(evt, ModelRebaseCompletedEvent)
    assert evt.success is False
    assert "protected_head_ref" in (evt.error or "")


@pytest.mark.asyncio
async def test_head_equals_base_refuses() -> None:
    """Refuse if head_ref == base_ref."""
    handler = HandlerRebaseEffect()
    evt = await handler.handle(
        ModelRebaseCommand(
            pr_number=301,
            repo="OmniNode-ai/omni_home",
            head_ref_name="feat/same",
            base_ref_name="feat/same",  # same as head!
            head_ref_oid="abc",
            correlation_id=_CORR_ID,
            run_id=_RUN_ID,
            total_prs=1,
        )
    )
    assert isinstance(evt, ModelRebaseCompletedEvent)
    assert evt.success is False
    assert "head_ref == base_ref" in (evt.error or "")


@pytest.mark.asyncio
async def test_missing_source_clone_returns_failure(tmp_path: Path) -> None:
    """If source clone doesn't exist, fail gracefully (not loud crash)."""
    handler = HandlerRebaseEffect()
    with patch.dict("os.environ", {"ONEX_REBASE_SOURCE_CLONE_ROOT": str(tmp_path)}):
        evt = await handler.handle(_cmd())

    assert isinstance(evt, ModelRebaseCompletedEvent)
    assert evt.success is False
    assert "Source clone not found" in (evt.error or "")


@pytest.mark.asyncio
async def test_missing_env_fails_loud() -> None:
    """Both ONEX_REBASE_SOURCE_CLONE_ROOT and OMNI_HOME unset → failure event (not exception)."""
    import os

    handler = HandlerRebaseEffect()
    env_without = {
        k: v
        for k, v in os.environ.items()
        if k not in ("ONEX_REBASE_SOURCE_CLONE_ROOT", "OMNI_HOME")
    }
    with patch.dict("os.environ", env_without, clear=True):
        evt = await handler.handle(_cmd())

    assert isinstance(evt, ModelRebaseCompletedEvent)
    assert evt.success is False
    assert "ONEX_REBASE_SOURCE_CLONE_ROOT" in (evt.error or "")


class _FakeGit:
    """Records every git argv and answers the rebase effect's git calls by content.

    Keyed by argv, not call order, so a guard that adds or moves a git call does
    not silently shift what the other answers mean.
    """

    def __init__(
        self,
        *,
        remote_head_sha: str = "abc123",
        conflict_files: list[str] | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self._remote_head_sha = remote_head_sha
        self._conflict_files = conflict_files

    async def run(self, *args: object, **kwargs: object) -> MagicMock:
        argv = [str(a) for a in args]
        self.calls.append(argv)
        proc = MagicMock()
        proc.returncode = 0
        stdout = b""
        stderr = b""
        if self._conflict_files is not None:
            if "rebase" in argv and "--abort" not in argv:
                proc.returncode = 1
                stderr = b"CONFLICT"
            elif "diff" in argv:
                stdout = "".join(f"{f}\n" for f in self._conflict_files).encode()
        if "rev-parse" in argv:
            stdout = (
                f"{self._remote_head_sha}\n".encode()
                if any(a.startswith("origin/") for a in argv)
                else b"newsha456\n"
            )
        proc.communicate = AsyncMock(return_value=(stdout, stderr))
        return proc

    def pushes(self) -> list[list[str]]:
        return [c for c in self.calls if "push" in c]

    def removed_worktree(self) -> bool:
        return any(c[3:5] == ["worktree", "remove"] for c in self.calls)


def _clone_dir_name() -> str:
    """The source-clone directory the handler derives from the command's repo slug."""
    return _cmd().repo.replace("/", "__")


def _source_clone(tmp_path: Path) -> None:
    (tmp_path / _clone_dir_name() / ".git").mkdir(parents=True)


@pytest.mark.asyncio
async def test_successful_rebase(tmp_path: Path) -> None:
    """Happy path: all git operations succeed → success=True, conflict_files=[]."""
    # The handler derives the source-clone directory from the repo slug.
    _source_clone(tmp_path)
    fake = _FakeGit()
    with (
        patch.dict(
            "os.environ",
            {
                "ONEX_REBASE_SOURCE_CLONE_ROOT": str(tmp_path),
                "ONEX_REBASE_WORKTREE_ROOT": str(tmp_path / "worktrees"),
            },
        ),
        patch("asyncio.create_subprocess_exec", side_effect=fake.run),
    ):
        evt = await HandlerRebaseEffect().handle(_cmd())

    assert isinstance(evt, ModelRebaseCompletedEvent)
    assert evt.success is True
    assert evt.conflict_files == []
    assert evt.actual_sha_after == "newsha456"
    assert evt.expected_sha_before == "abc123"
    assert fake.removed_worktree()


@pytest.mark.asyncio
async def test_rebase_conflict_aborts_and_records_files(tmp_path: Path) -> None:
    """Conflict during rebase → abort, conflict_files recorded, success=False, no push."""
    _source_clone(tmp_path)
    fake = _FakeGit(conflict_files=["a.py"])
    with (
        patch.dict(
            "os.environ",
            {
                "ONEX_REBASE_SOURCE_CLONE_ROOT": str(tmp_path),
                "ONEX_REBASE_WORKTREE_ROOT": str(tmp_path / "worktrees"),
            },
        ),
        patch("asyncio.create_subprocess_exec", side_effect=fake.run),
    ):
        evt = await HandlerRebaseEffect().handle(_cmd())

    assert isinstance(evt, ModelRebaseCompletedEvent)
    assert evt.success is False
    assert "a.py" in evt.conflict_files
    assert any(c for c in fake.calls if "--abort" in c)
    assert fake.pushes() == []
    assert fake.removed_worktree()


@pytest.mark.asyncio
@pytest.mark.parametrize("landing_branch", ["dev", "main", "master", "develop"])
async def test_landing_branch_head_refuses_without_git(landing_branch: str) -> None:
    """A rebase never pushes a landing branch, whatever the base is named."""
    fake = _FakeGit()
    with patch("asyncio.create_subprocess_exec", side_effect=fake.run):
        evt = await HandlerRebaseEffect().handle(
            _cmd(head_ref=landing_branch, base_ref="release")
        )

    assert evt.success is False
    assert "protected_head_ref" in (evt.error or "")
    assert fake.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "head_ref",
    [
        "refs/heads/feat/test",
        "refs/tags/v1",
        "-delete",
        "feat/../main",
        "feat//test",
        "feat/test.lock",
        "feat/test/",
        "feat test",
        "",
    ],
)
async def test_unqualified_or_malformed_head_ref_refuses_without_git(
    head_ref: str,
) -> None:
    """Only a plain branch name is ever pushed; a refspec-shaped name is refused."""
    fake = _FakeGit()
    with patch("asyncio.create_subprocess_exec", side_effect=fake.run):
        evt = await HandlerRebaseEffect().handle(_cmd(head_ref=head_ref))

    assert evt.success is False
    assert "invalid_head_ref" in (evt.error or "")
    assert fake.calls == []


@pytest.mark.asyncio
async def test_push_targets_only_the_pr_head_branch(tmp_path: Path) -> None:
    """Exactly one push, leased and addressed to the PR's own head branch ref."""
    _source_clone(tmp_path)
    fake = _FakeGit(remote_head_sha="abc123")
    with (
        patch.dict(
            "os.environ",
            {
                "ONEX_REBASE_SOURCE_CLONE_ROOT": str(tmp_path),
                "ONEX_REBASE_WORKTREE_ROOT": str(tmp_path / "worktrees"),
            },
        ),
        patch("asyncio.create_subprocess_exec", side_effect=fake.run),
    ):
        evt = await HandlerRebaseEffect().handle(_cmd(head_ref="feat/test"))

    assert evt.success is True
    pushes = fake.pushes()
    assert len(pushes) == 1
    push = pushes[0]
    assert "--force-with-lease=refs/heads/feat/test:abc123" in push
    assert push[-2:] == ["origin", "HEAD:refs/heads/feat/test"]
    assert not [
        a for a in push if a in ("--force", "-f", "--mirror", "--all", "--tags")
    ]


@pytest.mark.asyncio
async def test_head_that_moved_since_the_command_is_not_pushed(tmp_path: Path) -> None:
    """The remote ref must still be the PR head the command named, or nothing is pushed."""
    _source_clone(tmp_path)
    fake = _FakeGit(remote_head_sha="0ther5ha")
    with (
        patch.dict(
            "os.environ",
            {
                "ONEX_REBASE_SOURCE_CLONE_ROOT": str(tmp_path),
                "ONEX_REBASE_WORKTREE_ROOT": str(tmp_path / "worktrees"),
            },
        ),
        patch("asyncio.create_subprocess_exec", side_effect=fake.run),
    ):
        evt = await HandlerRebaseEffect().handle(_cmd(head_oid="abc123"))

    assert evt.success is False
    assert "head_moved" in (evt.error or "")
    assert fake.pushes() == []
    assert fake.removed_worktree()


def _git(cwd: Path, *args: str) -> str:
    import subprocess

    done = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            **scrub_git_location_env(os.environ),
            "HOME": str(cwd),
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.test",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.test",
        },
    )
    return done.stdout.strip()


@pytest.mark.asyncio
async def test_real_git_rebase_advances_only_the_pr_head_branch(tmp_path: Path) -> None:
    """Against real git: the PR head moves onto the new base and no other ref moves."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "-b", "dev")
    clones = tmp_path / "clones"
    clone = clones / _clone_dir_name()
    clone.mkdir(parents=True)
    _git(clone, "init", "-b", "dev")
    _git(clone, "remote", "add", "origin", str(origin))
    (clone / "base.txt").write_text("one\n")
    _git(clone, "add", "base.txt")
    _git(clone, "commit", "-m", "base")
    _git(clone, "push", "origin", "dev")
    _git(clone, "checkout", "-b", "feat/test")
    (clone / "feat.txt").write_text("feature\n")
    _git(clone, "add", "feat.txt")
    _git(clone, "commit", "-m", "feature")
    _git(clone, "push", "origin", "feat/test")
    head_before = _git(clone, "rev-parse", "HEAD")
    _git(clone, "checkout", "dev")
    (clone / "base.txt").write_text("two\n")
    _git(clone, "commit", "-am", "dev moves")
    _git(clone, "push", "origin", "dev")
    dev_tip = _git(origin, "rev-parse", "refs/heads/dev")
    refs_before = _git(origin, "for-each-ref", "--format=%(refname)")

    with patch.dict(
        "os.environ",
        {
            "ONEX_REBASE_SOURCE_CLONE_ROOT": str(clones),
            "ONEX_REBASE_WORKTREE_ROOT": str(tmp_path / "worktrees"),
            # The suite isolates git from global config; the rebase commits.
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.test",
        },
    ):
        evt = await HandlerRebaseEffect().handle(
            _cmd(head_ref="feat/test", base_ref="dev", head_oid=head_before)
        )

    assert evt.success is True, evt.error
    head_after = _git(origin, "rev-parse", "refs/heads/feat/test")
    assert head_after != head_before
    assert evt.actual_sha_after == head_after
    assert _git(origin, "merge-base", "--is-ancestor", dev_tip, head_after) == ""
    assert _git(origin, "rev-parse", "refs/heads/dev") == dev_tip
    assert _git(origin, "for-each-ref", "--format=%(refname)") == refs_before


def test_contract_declares_output_topic() -> None:
    """The rebase EFFECT contract declares the topic it publishes on."""
    import omnimarket.nodes.node_rebase_effect as node_pkg

    contract_text = (Path(node_pkg.__file__).parent / "contract.yaml").read_text()
    assert "onex.evt.omnimarket.pr-rebase-completed.v1" in contract_text
