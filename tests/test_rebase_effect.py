# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Task 6: Tests for node_rebase_effect [OMN-8961]."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from omnimarket.nodes.node_merge_sweep_triage_orchestrator.models.model_triage_request import (
    ModelRebaseCommand,
)
from omnimarket.nodes.node_rebase_effect.handlers.handler_rebase import (
    HandlerRebaseEffect,
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


@pytest.mark.asyncio
async def test_protected_head_ref_refuses_without_git(tmp_path: Path) -> None:
    """Rule: refuse to rebase if head_ref is a protected branch (main/master/develop)."""
    handler = HandlerRebaseEffect()
    # No git calls should happen — guard fires before any subprocess
    output = await handler.handle(
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
    assert len(output.events) == 1
    evt = output.events[0]
    assert isinstance(evt, ModelRebaseCompletedEvent)
    assert evt.success is False
    assert "protected_head_ref" in (evt.error or "")


@pytest.mark.asyncio
async def test_head_equals_base_refuses() -> None:
    """Refuse if head_ref == base_ref."""
    handler = HandlerRebaseEffect()
    output = await handler.handle(
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
    evt = output.events[0]
    assert isinstance(evt, ModelRebaseCompletedEvent)
    assert evt.success is False
    assert "head_ref == base_ref" in (evt.error or "")


@pytest.mark.asyncio
async def test_missing_source_clone_returns_failure(tmp_path: Path) -> None:
    """If source clone doesn't exist, fail gracefully (not loud crash)."""
    handler = HandlerRebaseEffect()
    with patch.dict("os.environ", {"ONEX_REBASE_SOURCE_CLONE_ROOT": str(tmp_path)}):
        output = await handler.handle(_cmd())

    evt = output.events[0]
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
        output = await handler.handle(_cmd())

    evt = output.events[0]
    assert isinstance(evt, ModelRebaseCompletedEvent)
    assert evt.success is False
    assert "ONEX_REBASE_SOURCE_CLONE_ROOT" in (evt.error or "")


@pytest.mark.asyncio
async def test_successful_rebase(tmp_path: Path) -> None:
    """Happy path: all git operations succeed → success=True, conflict_files=[]."""
    # Create a fake source clone with a .git dir.
    # Handler converts repo="OmniNode-ai/omni_home" → key "OmniNode-ai__omni_home".
    repo_dir = tmp_path / "OmniNode-ai__omni_home"
    (repo_dir / ".git").mkdir(parents=True)

    # git ops in order:
    # 1: worktree add
    # 2: fetch head_ref
    # 3: checkout head_ref
    # 4: fetch base_ref
    # 5: rebase
    # 6: push --force-with-lease
    # 7: rev-parse HEAD
    # 8: worktree remove (finally)
    call_count = 0

    async def fake_exec(*args: object, **kwargs: object) -> MagicMock:
        nonlocal call_count
        call_count += 1
        proc = MagicMock()
        proc.returncode = 0
        stdout = b""
        if call_count == 7:  # rev-parse HEAD
            stdout = b"newsha456\n"
        proc.communicate = AsyncMock(return_value=(stdout, b""))
        return proc

    with (
        patch.dict("os.environ", {"ONEX_REBASE_SOURCE_CLONE_ROOT": str(tmp_path)}),
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
    ):
        handler = HandlerRebaseEffect()
        output = await handler.handle(_cmd())

    evt = output.events[0]
    assert isinstance(evt, ModelRebaseCompletedEvent)
    assert evt.success is True
    assert evt.conflict_files == []
    assert evt.actual_sha_after == "newsha456"
    assert evt.expected_sha_before == "abc123"


@pytest.mark.asyncio
async def test_rebase_conflict_aborts_and_records_files(tmp_path: Path) -> None:
    """Conflict during rebase → abort, conflict_files recorded, success=False."""
    # Handler converts repo="OmniNode-ai/omni_home" → key "OmniNode-ai__omni_home".
    repo_dir = tmp_path / "OmniNode-ai__omni_home"
    (repo_dir / ".git").mkdir(parents=True)

    # git ops in order:
    # 1: worktree add, 2: fetch head, 3: checkout, 4: fetch base, 5: REBASE (fail)
    # 6: git diff --name-only (conflict files), 7: git rebase --abort, 8: worktree remove
    call_count = 0

    async def fake_exec(*args: object, **kwargs: object) -> MagicMock:
        nonlocal call_count
        call_count += 1
        proc = MagicMock()
        if call_count == 5:  # rebase fails with conflict
            proc.returncode = 1
            proc.communicate = AsyncMock(return_value=(b"", b"CONFLICT in a.py"))
        elif call_count == 6:  # git diff --name-only conflict files
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"a.py\n", b""))
        else:
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"", b""))
        return proc

    with (
        patch.dict("os.environ", {"ONEX_REBASE_SOURCE_CLONE_ROOT": str(tmp_path)}),
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
    ):
        handler = HandlerRebaseEffect()
        output = await handler.handle(_cmd())

    evt = output.events[0]
    assert isinstance(evt, ModelRebaseCompletedEvent)
    assert evt.success is False
    assert "a.py" in evt.conflict_files
