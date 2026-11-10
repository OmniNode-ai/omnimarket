# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_conflict_hunk_effect.

One conflict-hunk command in, one resolved event out, through the handler the node's
contract names (HandlerConflictHunk). The chain runs the real handler against a temp
source clone with an injected subprocess runner and LLM call: a conflicted file is
resolved, committed and pushed; a protected head ref is refused with no mutation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_conflict_hunk_effect.handlers.handler_conflict_hunk import (
    HandlerConflictHunk,
)
from omnimarket.nodes.node_conflict_hunk_effect.models.model_conflict_resolved_event import (
    ModelConflictResolvedEvent,
)
from tests.nodes.node_conflict_hunk_effect.test_handler_conflict_hunk import (
    _RESOLVED_CONTENT,
    _make_command,
    _make_subprocess_fn,
    _setup_source_clone,
)

_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_conflict_hunk_effect/contract.yaml"
)


def test_contract_names_the_handler_under_test() -> None:
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    assert contract["handler"]["class"] == HandlerConflictHunk.__name__
    assert contract["event_bus"]["subscribe_topics"] == [
        "onex.cmd.omnimarket.pr-conflict-hunk.v1"
    ]
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.pr-conflict-resolved.v1"
    ]


@pytest.mark.asyncio
async def test_golden_chain_command_resolves_commits_and_pushes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_source_clone(tmp_path)
    wt_base = tmp_path / "worktrees"
    wt_base.mkdir()
    monkeypatch.setenv("ONEX_CONFLICT_SOURCE_CLONE_ROOT", str(tmp_path))
    monkeypatch.setenv("ONEX_CONFLICT_WORKTREE_ROOT", str(wt_base))
    pushes: list[list[str]] = []
    seen: list[str] = []

    def llm_call(
        file_path: str, hunk_context: str, routing_policy: dict[str, Any]
    ) -> tuple[str, bool]:
        seen.append(file_path)
        return _RESOLVED_CONTENT, False

    handler = HandlerConflictHunk(
        llm_call_fn=llm_call,
        subprocess_run_fn=_make_subprocess_fn(
            wt_base, commit_sha="deadbeef1234", push_calls=pushes
        ),
    )
    command = _make_command()
    event = await handler.handle(command)

    assert isinstance(event, ModelConflictResolvedEvent)
    assert event.success is True
    assert event.resolution_committed is True
    assert event.resolved_files == ["src/foo.py"]
    assert event.correlation_id == command.correlation_id
    assert seen == ["src/foo.py"]
    assert len(pushes) == 1
    assert any(arg.startswith("--force-with-lease=") for arg in pushes[0])


@pytest.mark.asyncio
async def test_golden_chain_error_leg_protected_head_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_source_clone(tmp_path)
    monkeypatch.setenv("ONEX_CONFLICT_SOURCE_CLONE_ROOT", str(tmp_path))
    monkeypatch.setenv("ONEX_CONFLICT_WORKTREE_ROOT", str(tmp_path / "wt"))
    pushes: list[list[str]] = []

    handler = HandlerConflictHunk(
        llm_call_fn=lambda *_: (_RESOLVED_CONTENT, False),
        subprocess_run_fn=_make_subprocess_fn(tmp_path, push_calls=pushes),
    )
    event = await handler.handle(_make_command(head_ref="main"))

    assert event.success is False
    assert event.resolution_committed is False
    assert event.error is not None
    assert "protected_head_ref" in event.error
    assert pushes == []
