# SPDX-License-Identifier: MIT
"""Integration test for HandlerHandoffEffect.

Invokes the handler in a real temp git repo and asserts the artifact is
written with all expected fields.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from omnimarket.nodes.node_handoff_effect.handlers.handler_handoff_effect import (
    HandlerHandoffEffect,
    InfraHealthGatherError,
    _parse_env_sync_log,
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


def _mock_ssh_ok(remote_cmd: str, check_name: str) -> str:
    return "ok"


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

        with patch(
            "omnimarket.nodes.node_handoff_effect.handlers.handler_handoff_effect._ssh",
            side_effect=_mock_ssh_ok,
        ):
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
        with patch(
            "omnimarket.nodes.node_handoff_effect.handlers.handler_handoff_effect._ssh",
            side_effect=_mock_ssh_ok,
        ):
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
        with patch(
            "omnimarket.nodes.node_handoff_effect.handlers.handler_handoff_effect._ssh",
            side_effect=_mock_ssh_ok,
        ):
            result = handler.handle(
                session_id="sess-nogit",
                correlation_id=uuid.uuid4(),
                cwd=str(non_git),
            )

        assert Path(result["artifact_path"]).exists()
        assert result["captured_branches"] == []
        assert result["captured_dirty_files"] == []


class TestParseEnvSyncLog:
    def test_no_success_line_returns_never(self, tmp_path: Path) -> None:
        log = tmp_path / "env-sync.log"
        log.write_text(
            "2026-04-15T10:00:00Z FAILURE seed-infisical exit=1\n"
            "2026-04-15T10:05:00Z FAILURE seed-infisical exit=1\n"
        )
        result = _parse_env_sync_log(log)
        assert result["seed_infisical_last_success"] == "NEVER"

    def test_success_line_extracts_timestamp(self, tmp_path: Path) -> None:
        log = tmp_path / "env-sync.log"
        log.write_text(
            "2026-04-15T09:00:00Z FAILURE seed-infisical exit=1\n"
            "2026-04-15T10:00:00Z SUCCESS seed-infisical exit=0\n"
            "2026-04-15T10:05:00Z FAILURE seed-infisical exit=1\n"
        )
        result = _parse_env_sync_log(log)
        assert result["seed_infisical_last_success"] == "2026-04-15T10:00:00Z"

    def test_last_run_captures_most_recent_line(self, tmp_path: Path) -> None:
        log = tmp_path / "env-sync.log"
        log.write_text(
            "2026-04-15T10:00:00Z SUCCESS seed-infisical exit=0\n"
            "2026-04-15T10:05:00Z FAILURE seed-infisical exit=1\n"
        )
        result = _parse_env_sync_log(log)
        assert "2026-04-15T10:05:00Z" in result["seed_infisical_last_run"]
        assert "exit=1" in result["seed_infisical_last_run"]

    def test_missing_log_file_returns_unknown(self, tmp_path: Path) -> None:
        log = tmp_path / "nonexistent.log"
        result = _parse_env_sync_log(log)
        assert result["seed_infisical_last_success"] == "NEVER"
        assert "unknown" in result["seed_infisical_last_run"].lower()


class TestInfraHealthGatherError:
    def test_ssh_failure_raises_infra_health_gather_error(
        self, git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state_dir = tmp_path / "onex_state"
        monkeypatch.setenv("ONEX_STATE_DIR", str(state_dir))

        log_dir = state_dir / "logs"
        log_dir.mkdir(parents=True)
        (log_dir / "env-sync.log").write_text(
            "2026-04-15T10:00:00Z SUCCESS seed-infisical exit=0\n"
        )

        # Patch _ssh directly so git subprocess calls are unaffected
        with patch(
            "omnimarket.nodes.node_handoff_effect.handlers.handler_handoff_effect._ssh",
            side_effect=InfraHealthGatherError(
                "SSH check 'infisical' timed out after 15s"
            ),
        ):
            handler = HandlerHandoffEffect()
            with pytest.raises(InfraHealthGatherError, match="infisical"):
                handler.handle(
                    session_id="sess-ssh-fail",
                    correlation_id=uuid.uuid4(),
                    cwd=str(git_repo),
                )

    def test_artifact_contains_infra_health_section(
        self, git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state_dir = tmp_path / "onex_state"
        monkeypatch.setenv("ONEX_STATE_DIR", str(state_dir))
        monkeypatch.setenv("ONEX_INFRA_BLOCKER_TICKETS", "OMN-9999")

        log_dir = state_dir / "logs"
        log_dir.mkdir(parents=True)
        (log_dir / "env-sync.log").write_text(
            "2026-04-15T10:00:00Z SUCCESS seed-infisical exit=0\n"
        )

        def _mock_ssh(remote_cmd: str, check_name: str) -> str:
            if check_name == "infisical":
                return "infisical.Up 2 hours"
            if check_name == "deploy-agent":
                return "active"
            if check_name == "runtime-effects":
                return "healthy"
            if check_name == "kafka-redpanda":
                return "Up 3 hours"
            if check_name == "postgres":
                return "Up 3 hours"
            return "unknown"

        handler = HandlerHandoffEffect()
        with patch(
            "omnimarket.nodes.node_handoff_effect.handlers.handler_handoff_effect._ssh",
            side_effect=_mock_ssh,
        ):
            result = handler.handle(
                session_id="sess-infra",
                correlation_id=uuid.uuid4(),
                cwd=str(git_repo),
            )

        data = yaml.safe_load(Path(result["artifact_path"]).read_text())
        ih = data["infra_health"]
        assert ih["seed_infisical_last_success"] == "2026-04-15T10:00:00Z"
        assert ih["infisical_container"] == "infisical.Up 2 hours"
        assert ih["deploy_agent_service"] == "active"
        assert ih["runtime_effects_health"] == "healthy"
        assert ih["kafka_redpanda"] == "Up 3 hours"
        assert ih["postgres"] == "Up 3 hours"
        assert ih["open_infra_blockers"] == "OMN-9999"
