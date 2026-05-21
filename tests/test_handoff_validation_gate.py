# SPDX-License-Identifier: MIT
"""Unit tests for the handoff validation gate (OMN-11266).

TDD: these tests are written against the expected interface BEFORE implementation.

Gate criteria: abort with exit code 2 when any of:
  - SSH to .201 times out or returns non-zero
  - env-sync.log is missing or unreadable
  - Any required service probe returns no output (unhealthy)
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from omnimarket.nodes.node_handoff_effect.handlers.handler_handoff_effect import (
    HandlerHandoffEffect,
    HandoffGateError,
    validate_infra_sources,
)

_SSH_TARGET = "jonah@192.168.86.201"  # onex-allow-internal-ip OMN-11266 reason="test fixture: known .201 SSH target for gate validation tests"
_PATCH_SSH_PROBE = "omnimarket.nodes.node_handoff_effect.service_catalog._run_ssh_probe"
_PATCH_SSH = "omnimarket.nodes.node_handoff_effect.handlers.handler_handoff_effect._ssh"


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    _git(["init"], tmp_path)
    _git(["config", "user.email", "test@test.com"], tmp_path)
    _git(["config", "user.name", "Test"], tmp_path)
    readme = tmp_path / "README.md"
    readme.write_text("hello\n")
    _git(["add", "README.md"], tmp_path)
    _git(["commit", "-m", "init"], tmp_path)
    return tmp_path


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    d = tmp_path / "state"
    d.mkdir()
    return d


@pytest.fixture
def valid_log(state_dir: Path) -> Path:
    log_dir = state_dir / "logs"
    log_dir.mkdir(parents=True)
    log = log_dir / "env-sync.log"
    log.write_text("2026-05-21T10:00:00Z SUCCESS seed-infisical exit=0\n")
    return log


def _healthy_ssh_probe_mock(
    ssh_target: str, remote_cmd: str, timeout_s: int
) -> tuple[str, str | None]:
    if "postgres" in remote_cmd:
        return "postgres|Up 3 hours", None
    if "redpanda" in remote_cmd:
        return "redpanda|Up 3 hours", None
    if "valkey" in remote_cmd:
        return "valkey|Up 3 hours", None
    if "infisical" in remote_cmd:
        return "infisical|Up 2 hours", None
    if "inspect omninode-runtime-effects" in remote_cmd:
        return "healthy", None
    if "deploy-agent.service" in remote_cmd:
        return "active", None
    return "", "unknown probe"


def _mock_ssh_ok(remote_cmd: str, check_name: str, ssh_host: str = "") -> str:
    return "ok"


@pytest.mark.unit
class TestHandoffGateError:
    """validate_infra_sources raises HandoffGateError on each failure mode."""

    def test_ssh_timeout_raises_gate_error(
        self, valid_log: Path, state_dir: Path
    ) -> None:
        with (
            patch(
                _PATCH_SSH_PROBE,
                return_value=("", "timed out after 5s"),
            ),
            pytest.raises(HandoffGateError) as exc_info,
        ):
            validate_infra_sources(
                env_sync_log_path=valid_log,
                ssh_target=_SSH_TARGET,
            )
        assert "HANDOFF_GATE_FAILURE" in str(exc_info.value)
        assert exc_info.value.exit_code == 2

    def test_missing_env_sync_log_raises_gate_error(self, tmp_path: Path) -> None:
        missing_log = tmp_path / "nonexistent" / "env-sync.log"
        with (
            patch(_PATCH_SSH_PROBE, side_effect=_healthy_ssh_probe_mock),
            pytest.raises(HandoffGateError) as exc_info,
        ):
            validate_infra_sources(
                env_sync_log_path=missing_log,
                ssh_target=_SSH_TARGET,
            )
        assert "HANDOFF_GATE_FAILURE" in str(exc_info.value)
        assert "env-sync.log" in str(exc_info.value)
        assert exc_info.value.exit_code == 2

    def test_partial_failure_blocks_gate(self, valid_log: Path) -> None:
        """5 of 6 probes healthy, 1 fails → gate must block."""
        call_count = 0

        def _partial_failure(
            ssh_target: str, remote_cmd: str, timeout_s: int
        ) -> tuple[str, str | None]:
            nonlocal call_count
            call_count += 1
            if "deploy-agent.service" in remote_cmd:
                return "", "timed out after 5s"
            return _healthy_ssh_probe_mock(ssh_target, remote_cmd, timeout_s)

        with (
            patch(_PATCH_SSH_PROBE, side_effect=_partial_failure),
            pytest.raises(HandoffGateError) as exc_info,
        ):
            validate_infra_sources(
                env_sync_log_path=valid_log,
                ssh_target=_SSH_TARGET,
            )
        assert exc_info.value.exit_code == 2
        assert "deploy-agent.service" in str(exc_info.value)

    def test_all_probes_succeed_no_error(self, valid_log: Path) -> None:
        with patch(_PATCH_SSH_PROBE, side_effect=_healthy_ssh_probe_mock):
            validate_infra_sources(
                env_sync_log_path=valid_log,
                ssh_target=_SSH_TARGET,
            )


@pytest.mark.unit
class TestHandlerHandoffEffectGate:
    """HandlerHandoffEffect.handle() must not write artifact when gate blocks."""

    def test_ssh_timeout_no_artifact_written(
        self, git_repo: Path, state_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ONEX_STATE_DIR", str(state_dir))
        monkeypatch.setenv("ONEX_INFRA_SSH_TARGET", _SSH_TARGET)
        log_dir = state_dir / "logs"
        log_dir.mkdir(parents=True)
        (log_dir / "env-sync.log").write_text(
            "2026-05-21T10:00:00Z SUCCESS seed-infisical exit=0\n"
        )

        with patch(_PATCH_SSH_PROBE, return_value=("", "timed out after 5s")):
            handler = HandlerHandoffEffect()
            with pytest.raises(HandoffGateError):
                handler.handle(
                    session_id="sess-timeout",
                    correlation_id=uuid.uuid4(),
                    cwd=str(git_repo),
                )

        handoff_dir = state_dir / "session" / "handoff"
        written = (
            list(handoff_dir.glob("handoff-*.yaml")) if handoff_dir.exists() else []
        )
        assert written == [], f"Expected no artifact, found: {written}"

    def test_missing_env_sync_log_exit_code_2(
        self, git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state_dir = tmp_path / "empty_state"
        state_dir.mkdir()
        monkeypatch.setenv("ONEX_STATE_DIR", str(state_dir))
        monkeypatch.setenv("ONEX_INFRA_SSH_TARGET", _SSH_TARGET)

        with patch(_PATCH_SSH_PROBE, side_effect=_healthy_ssh_probe_mock):
            handler = HandlerHandoffEffect()
            with pytest.raises(HandoffGateError) as exc_info:
                handler.handle(
                    session_id="sess-nolog",
                    correlation_id=uuid.uuid4(),
                    cwd=str(git_repo),
                )

        assert exc_info.value.exit_code == 2
        assert "HANDOFF_GATE_FAILURE" in str(exc_info.value)

    def test_partial_probe_failure_no_artifact(
        self, git_repo: Path, state_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ONEX_STATE_DIR", str(state_dir))
        monkeypatch.setenv("ONEX_INFRA_SSH_TARGET", _SSH_TARGET)
        log_dir = state_dir / "logs"
        log_dir.mkdir(parents=True)
        (log_dir / "env-sync.log").write_text(
            "2026-05-21T10:00:00Z SUCCESS seed-infisical exit=0\n"
        )

        def _five_healthy_one_fails(
            ssh_target: str, remote_cmd: str, timeout_s: int
        ) -> tuple[str, str | None]:
            if "inspect omninode-runtime-effects" in remote_cmd:
                return "", "exit 1: no such container"
            return _healthy_ssh_probe_mock(ssh_target, remote_cmd, timeout_s)

        with patch(_PATCH_SSH_PROBE, side_effect=_five_healthy_one_fails):
            handler = HandlerHandoffEffect()
            with pytest.raises(HandoffGateError):
                handler.handle(
                    session_id="sess-partial",
                    correlation_id=uuid.uuid4(),
                    cwd=str(git_repo),
                )

        handoff_dir = state_dir / "session" / "handoff"
        written = (
            list(handoff_dir.glob("handoff-*.yaml")) if handoff_dir.exists() else []
        )
        assert written == [], f"Expected no artifact, found: {written}"

    def test_all_healthy_artifact_written(
        self, git_repo: Path, state_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ONEX_STATE_DIR", str(state_dir))
        monkeypatch.setenv("ONEX_INFRA_SSH_TARGET", _SSH_TARGET)
        log_dir = state_dir / "logs"
        log_dir.mkdir(parents=True)
        (log_dir / "env-sync.log").write_text(
            "2026-05-21T10:00:00Z SUCCESS seed-infisical exit=0\n"
        )

        with (
            patch(_PATCH_SSH_PROBE, side_effect=_healthy_ssh_probe_mock),
            patch(_PATCH_SSH, side_effect=_mock_ssh_ok),
        ):
            handler = HandlerHandoffEffect()
            result = handler.handle(
                session_id="sess-ok",
                correlation_id=uuid.uuid4(),
                cwd=str(git_repo),
            )

        assert Path(result["artifact_path"]).exists()
