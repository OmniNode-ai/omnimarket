# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A session may run in a caller-named workspace, validated per request (OMN-26221)."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnimarket.models.delegation.model_delegation_target import (
    ModelDelegationTargetRequest,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_delegation_target import (
    HandlerDelegationTarget,
)

pytestmark = pytest.mark.unit


def request(**updates: object) -> ModelDelegationTargetRequest:
    values: dict[str, object] = {
        "request_id": str(uuid4()),
        "target": "claude-code",
        "brief": "Return the answer.",
        "call_site": "lab_job",
    }
    values.update(updates)
    return ModelDelegationTargetRequest.model_validate(values)


def handler(tmp_path: Path, roots: list[str]) -> HandlerDelegationTarget:
    config = {
        "allowed_owners": ["OmniNode-ai"],
        "repository_root": str(tmp_path / "clones"),
        "work_root": str(tmp_path / "work"),
        "call_sites": {
            "lab_job": {
                "targets": ["claude-code", "codex"],
                "timeout_seconds": 30,
                "allowed_workspace_roots": roots,
                "agents": {"claude-code": {"model": "m"}},
            }
        },
    }
    return HandlerDelegationTarget(event_bus=None, config=config)


def fake_claude(directory: Path) -> None:
    binary = directory / "claude"
    binary.write_text(
        "#!/bin/sh\ncat >/dev/null\n"
        'printf \'{"type":"result","result":"ran in %s","session_id":"s1"}\' "$(pwd -P)"\n'
    )
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)


def test_workspace_path_excludes_repo_and_commit() -> None:
    with pytest.raises(ValidationError):
        request(
            workspace_path="/tmp/x", repo="OmniNode-ai/omnimarket", commit_sha="a" * 40
        )


def test_session_needs_a_workspace_or_a_checkout() -> None:
    with pytest.raises(ValidationError):
        request()


@pytest.mark.parametrize("kind", ["outside", "root_itself", "escape", "missing"])
async def test_workspace_outside_allowed_roots_is_refused(
    tmp_path: Path, kind: str
) -> None:
    root = tmp_path / "trees"
    root.mkdir()
    (tmp_path / "other").mkdir()
    (root / "link").symlink_to(tmp_path / "other")
    path = {
        "outside": str(tmp_path / "other"),
        "root_itself": str(root),
        "escape": str(root / "link"),
        "missing": str(root / "absent"),
    }[kind]
    with pytest.raises(ValueError, match="workspace"):
        await handler(tmp_path, [str(root)]).handle(request(workspace_path=path))
    assert not (tmp_path / "work").exists()


async def test_no_declared_roots_refuses_every_workspace(tmp_path: Path) -> None:
    tree = tmp_path / "trees" / "t"
    tree.mkdir(parents=True)
    with pytest.raises(ValueError, match="workspace"):
        await handler(tmp_path, []).handle(request(workspace_path=str(tree)))


async def test_validated_workspace_is_the_session_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "trees"
    tree = root / "ticket" / "repo"
    tree.mkdir(parents=True)
    fake_claude(tmp_path)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    result = await handler(tmp_path, [str(root)]).handle(
        request(workspace_path=str(tree))
    )
    assert result.status == "completed"
    assert result.worktree == str(tree.resolve())
    assert str(tree.resolve()) in result.content
    assert result.commit_sha is None
