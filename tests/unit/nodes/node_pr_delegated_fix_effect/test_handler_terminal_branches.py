# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Document outcomes, fallback, and every terminal's acceptance denominator."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from omnimarket.events.pr_delegated_fix import (
    EnumDelegatedFixOutcome,
    ModelDelegatedFixCommand,
)
from omnimarket.nodes.node_pr_delegated_fix_effect.handlers.adapter_acceptance_telemetry import (
    EnumPlacementReason,
    ModelDelegatedFixAttemptRecord,
)
from omnimarket.nodes.node_pr_delegated_fix_effect.handlers.adapter_document_delegation import (
    DocumentDelegationOutcome,
)
from omnimarket.nodes.node_pr_delegated_fix_effect.handlers.handler_delegated_fix import (
    HandlerDelegatedFix,
    PrPolishRunOutcome,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def command(tmp_path: Path) -> ModelDelegatedFixCommand:
    (tmp_path / ".git").write_text("gitdir: /tmp/fixture\n", encoding="utf-8")
    return ModelDelegatedFixCommand(
        correlation_id=uuid4(),
        repo="OmniNode-ai/omnimarket",
        pr_number=500,
        block_reason="code_failure",
        changed_files=["src/example.py"],
        diff_total_lines=5,
        worktree_path=str(tmp_path),
        requested_at=datetime.now(UTC),
    )


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("local", EnumDelegatedFixOutcome.ACCEPTED),
        ("escalated", EnumDelegatedFixOutcome.ACCEPTED),
        ("empty-commit", EnumDelegatedFixOutcome.ACCEPTED),
        ("no-changes", EnumDelegatedFixOutcome.NO_CHANGES),
        ("line-limit", EnumDelegatedFixOutcome.REFUSED_SIZE_GATE),
        ("denylist", EnumDelegatedFixOutcome.REFUSED_DENYLIST),
        ("commit-failed", EnumDelegatedFixOutcome.ERROR),
        ("delegation-failed", EnumDelegatedFixOutcome.ERROR),
        ("gate-failed", EnumDelegatedFixOutcome.GATE_FAILED),
    ],
)
async def test_document_terminals_record_exactly_one_sample(
    tmp_path: Path,
    command: ModelDelegatedFixCommand,
    scenario: str,
    expected: EnumDelegatedFixOutcome,
) -> None:
    tier = "cloud" if scenario == "escalated" else "local"
    document = Mock(
        run=AsyncMock(
            return_value=DocumentDelegationOutcome(
                "document-model", 0.02, "document-backend", tier, 1
            )
        )
    )
    if scenario == "delegation-failed":
        document.run.side_effect = TimeoutError("delegation timed out")
    files = ["src/example.py"]
    if scenario == "no-changes":
        files = []
    elif scenario == "denylist":
        files = ["onex_change_control/contracts/example.yaml"]
    git = Mock()
    git.changed_files.return_value = files
    git.diff_line_count.return_value = 1000 if scenario == "line-limit" else 6
    git.commit_all.return_value = "" if scenario == "empty-commit" else "abc12345"
    if scenario == "commit-failed":
        git.commit_all.side_effect = RuntimeError("commit hook failed")
    polish = Mock(
        run=Mock(
            return_value=PrPolishRunOutcome(
                "failed" if scenario == "gate-failed" else "done", None
            )
        )
    )
    ruff = Mock()
    recorder = Mock()
    handler = HandlerDelegatedFix(
        worktree_resolver=Mock(resolve=Mock(return_value=tmp_path)),
        ruff_runner=ruff,
        git_diff_adapter=git,
        pr_polish_runner=polish,
        document_delegation_runner=document,
        diff_classifier=Mock(is_document_class=Mock(return_value=True)),
        acceptance_recorder=recorder,
    )

    result = await handler.handle(command)

    assert result.outcome == expected
    assert result.is_success is (expected == EnumDelegatedFixOutcome.ACCEPTED)
    ruff.run.assert_not_called()
    document.run.assert_awaited_once_with(tmp_path, changed_files=command.changed_files)
    recorder.record.assert_called_once()
    row = recorder.record.call_args.args[0]
    assert isinstance(row, ModelDelegatedFixAttemptRecord)
    assert row.outcome == expected.value
    assert row.accepted is result.is_success
    assert row.correlation_id == command.correlation_id
    assert row.recorded_at == result.completed_at
    assert row.files_changed == result.files_changed
    assert row.lines_changed == result.lines_changed
    if scenario == "delegation-failed":
        assert result.error == "delegation timed out"
        assert row.task_type is None
        assert row.cost_usd == 0.0
        git.changed_files.assert_not_called()
    else:
        assert row.task_type == "document"
        assert row.delegation_model == "document-model"
        assert row.backend_id == "document-backend"
        assert row.cost_usd == 0.02
        assert row.placement_reason == (
            EnumPlacementReason.FALLBACK
            if tier == "cloud"
            else EnumPlacementReason.LOCAL_FIRST
        )
    if scenario in {"denylist", "line-limit"}:
        git.discard_changes.assert_called_once_with(tmp_path)
        git.commit_all.assert_not_called()
    else:
        git.discard_changes.assert_not_called()
    if scenario in {"local", "escalated", "empty-commit", "gate-failed"}:
        assert "delegated-by: document-model" in git.commit_all.call_args.args[1]
        assert command.repo in git.commit_all.call_args.args[1]
        polish.run.assert_called_once_with(
            repo=command.repo,
            pr_number=command.pr_number,
            ticket_id=None,
            worktree=tmp_path,
            dry_run=False,
        )
        assert result.commit_sha == git.commit_all.return_value
    else:
        polish.run.assert_not_called()


async def test_classifier_exception_falls_back_to_ruff(
    tmp_path: Path,
    command: ModelDelegatedFixCommand,
) -> None:
    ruff = Mock()
    document = Mock(run=AsyncMock())
    recorder = Mock()
    handler = HandlerDelegatedFix(
        worktree_resolver=Mock(resolve=Mock(return_value=tmp_path)),
        ruff_runner=ruff,
        git_diff_adapter=Mock(changed_files=Mock(return_value=[])),
        document_delegation_runner=document,
        diff_classifier=Mock(
            is_document_class=Mock(side_effect=RuntimeError("bad classifier"))
        ),
        acceptance_recorder=recorder,
    )
    result = await handler.handle(command)
    assert result.outcome == EnumDelegatedFixOutcome.NO_CHANGES
    ruff.run.assert_called_once_with(tmp_path)
    document.run.assert_not_awaited()
    row = recorder.record.call_args.args[0]
    assert row.task_type is None
    assert row.delegation_model == "ruff-deterministic"


async def test_recorder_exception_cannot_change_terminal_outcome(
    tmp_path: Path,
    command: ModelDelegatedFixCommand,
) -> None:
    recorder = Mock(record=Mock(side_effect=RuntimeError("sink down")))
    handler = HandlerDelegatedFix(
        worktree_resolver=Mock(resolve=Mock(return_value=tmp_path)),
        ruff_runner=Mock(),
        git_diff_adapter=Mock(changed_files=Mock(return_value=[])),
        document_delegation_runner=None,
        acceptance_recorder=recorder,
    )
    assert (await handler.handle(command)).outcome == EnumDelegatedFixOutcome.NO_CHANGES
    recorder.record.assert_called_once()


async def test_missing_state_directory_disables_telemetry_only(
    tmp_path: Path,
    command: ModelDelegatedFixCommand,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ONEX_STATE_DIR", raising=False)
    monkeypatch.delenv("OMNI_HOME", raising=False)
    handler = HandlerDelegatedFix(
        worktree_resolver=Mock(resolve=Mock(return_value=tmp_path)),
        ruff_runner=Mock(),
        git_diff_adapter=Mock(changed_files=Mock(return_value=[])),
        document_delegation_runner=None,
    )
    assert (await handler.handle(command)).outcome == EnumDelegatedFixOutcome.NO_CHANGES


def test_runtime_sync_shim_and_handler_identity(
    tmp_path: Path,
    command: ModelDelegatedFixCommand,
) -> None:
    handler = HandlerDelegatedFix(
        worktree_resolver=Mock(resolve=Mock(return_value=tmp_path)),
        ruff_runner=Mock(),
        git_diff_adapter=Mock(changed_files=Mock(return_value=[])),
        document_delegation_runner=None,
        acceptance_recorder=Mock(),
    )
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        assert (
            handler.handle_sync(command).outcome == EnumDelegatedFixOutcome.NO_CHANGES
        )
    finally:
        loop.close()
        asyncio.set_event_loop(None)
    assert handler.handler_type == "NODE_HANDLER"
    assert handler.handler_category == "EFFECT"
