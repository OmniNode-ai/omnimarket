# SPDX-License-Identifier: MIT
"""Integration test for HandlerHandoffEffect.

Invokes the handler in a real temp git repo and asserts the artifact is
written with all expected fields.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_handoff_effect.handlers.handler_handoff_effect import (
    HandlerHandoffEffect,
)


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo with one commit and one dirty file."""
    _git(["init"], tmp_path)
    _git(["config", "user.email", "test@test.com"], tmp_path)
    _git(["config", "user.name", "Test"], tmp_path)
    readme = tmp_path / "README.md"
    readme.write_text("hello\n")
    _git(["add", "README.md"], tmp_path)
    _git(["commit", "-m", "init"], tmp_path)
    (tmp_path / "dirty.py").write_text("x = 1\n")
    return tmp_path


@pytest.mark.integration
class TestHandlerHandoffEffect:
    def test_artifact_written_with_expected_fields(
        self, git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state_dir = tmp_path / "onex_state"
        monkeypatch.setenv("ONEX_STATE_DIR", str(state_dir))

        handler = HandlerHandoffEffect()
        session_id = "test-sess-abc123"
        correlation_id = uuid.uuid4()

        result = handler.handle(
            session_id=session_id,
            correlation_id=correlation_id,
            summary="continue auth work",
            cwd=str(git_repo),
        )

        artifact_path = Path(result["artifact_path"])
        assert artifact_path.exists(), f"Artifact not written: {artifact_path}"

        data = yaml.safe_load(artifact_path.read_text())
        assert data["version"] == 1
        assert data["session_id"] == session_id
        assert data["correlation_id"] == str(correlation_id)
        assert data["summary"] == "continue auth work"
        assert data["cwd"] == str(git_repo)
        assert isinstance(data["cwd_hash"], str)
        assert len(data["cwd_hash"]) == 8
        assert "context" in data
        ctx = data["context"]
        assert isinstance(ctx["recent_commits"], list)
        assert len(ctx["recent_commits"]) >= 1
        assert isinstance(ctx["dirty_files"], list)

    def test_captured_outputs_match_git_state(
        self, git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path / "state"))

        handler = HandlerHandoffEffect()
        result = handler.handle(
            session_id="sess-xyz",
            correlation_id=uuid.uuid4(),
            cwd=str(git_repo),
        )

        assert isinstance(result["captured_branches"], list)
        assert len(result["captured_branches"]) >= 1
        assert isinstance(result["captured_dirty_files"], list)
        assert any("dirty.py" in f for f in result["captured_dirty_files"])

    def test_non_git_dir_does_not_raise(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        non_git = tmp_path / "not_a_repo"
        non_git.mkdir()
        monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path / "state"))

        handler = HandlerHandoffEffect()
        result = handler.handle(
            session_id="sess-nogit",
            correlation_id=uuid.uuid4(),
            cwd=str(non_git),
        )

        assert Path(result["artifact_path"]).exists()
        assert result["captured_branches"] == []
        assert result["captured_dirty_files"] == []
