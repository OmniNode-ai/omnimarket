# SPDX-License-Identifier: MIT
"""Failure-mode tests for handoff pipeline when .201 is unreachable (OMN-11275).

Covers scenarios not addressed in test_handoff_validation_gate.py:
  - SSH connection refused (OSError path in _run_ssh_probe)
  - ONEX_INFRA_SSH_TARGET unconfigured
  - stderr output contains HANDOFF_GATE_FAILURE on gate failure
  - env-sync.log present but no SUCCESS line → artifact reflects "NEVER"
  - Each individual service probe failing in isolation
  - Strict gate mode: any single probe failure blocks even when others pass
"""

from __future__ import annotations

import io
import subprocess
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from omnimarket.nodes.node_handoff_effect.handlers.handler_handoff_effect import (
    HandlerHandoffEffect,
    HandoffGateError,
    _parse_env_sync_log,
    validate_infra_sources,
)
from omnimarket.nodes.node_handoff_effect.service_catalog import (
    ServiceCatalogSnapshot,
    ServiceProbeResult,
    _run_ssh_probe,
    probe_services,
)

_SSH_TARGET = "jonah@192.168.86.201"  # onex-allow-internal-ip OMN-11275 reason="test fixture: known .201 SSH target for failure-mode gate tests"
_PATCH_SSH_PROBE = "omnimarket.nodes.node_handoff_effect.service_catalog._run_ssh_probe"
_PATCH_SSH = "omnimarket.nodes.node_handoff_effect.handlers.handler_handoff_effect._ssh"

_ALL_SERVICES = (
    "postgres",
    "kafka",
    "valkey",
    "infisical",
    "omninode-runtime-effects",
    "deploy-agent.service",
)


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


def _healthy_probe_mock(
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


def _mock_ssh_ok(remote_cmd: str, check_name: str) -> str:
    return "ok"


@pytest.mark.unit
class TestConnectionRefused:
    """SSH connection refused surfaces as gate failure, not a silent skip."""

    def test_connection_refused_raises_gate_error(self, valid_log: Path) -> None:
        with (
            patch(
                _PATCH_SSH_PROBE,
                return_value=("", "Connection refused"),
            ),
            pytest.raises(HandoffGateError) as exc_info,
        ):
            validate_infra_sources(
                env_sync_log_path=valid_log,
                ssh_target=_SSH_TARGET,
            )
        assert exc_info.value.exit_code == 2
        assert "HANDOFF_GATE_FAILURE" in str(exc_info.value)

    def test_connection_refused_osexception_in_probe(self, valid_log: Path) -> None:
        """OSError in _run_ssh_probe itself (e.g. 'ssh: not found') blocks gate."""
        with (
            patch(
                _PATCH_SSH_PROBE,
                side_effect=OSError("ssh: No such file or directory"),
            ),
            pytest.raises((HandoffGateError, OSError)),
        ):
            validate_infra_sources(
                env_sync_log_path=valid_log,
                ssh_target=_SSH_TARGET,
            )

    def test_run_ssh_probe_osexception_returns_error_string(self) -> None:
        """_run_ssh_probe catches OSError and returns ("", error_string)."""
        with patch(
            "subprocess.run",
            side_effect=OSError("Connection refused"),
        ):
            output, error = _run_ssh_probe(
                ssh_target=_SSH_TARGET,
                remote_cmd="echo hi",
                timeout_s=5,
            )
        assert output == ""
        assert error is not None
        assert "Connection refused" in error

    def test_run_ssh_probe_timeout_returns_error_string(self) -> None:
        """_run_ssh_probe catches TimeoutExpired and returns ("", error_string)."""
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["ssh"], timeout=5),
        ):
            output, error = _run_ssh_probe(
                ssh_target=_SSH_TARGET,
                remote_cmd="echo hi",
                timeout_s=5,
            )
        assert output == ""
        assert error is not None
        assert "timed out" in error


@pytest.mark.unit
class TestUnconfiguredSshTarget:
    """Empty ONEX_INFRA_SSH_TARGET blocks the gate before any probe is attempted."""

    def test_empty_ssh_target_raises_gate_error(self, valid_log: Path) -> None:
        with pytest.raises(HandoffGateError) as exc_info:
            validate_infra_sources(
                env_sync_log_path=valid_log,
                ssh_target="",
            )
        assert exc_info.value.exit_code == 2
        assert "HANDOFF_GATE_FAILURE" in str(exc_info.value)
        assert "not configured" in str(exc_info.value)

    def test_unconfigured_target_no_artifact_written(
        self,
        git_repo: Path,
        state_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ONEX_STATE_DIR", str(state_dir))
        monkeypatch.delenv("ONEX_INFRA_SSH_TARGET", raising=False)
        log_dir = state_dir / "logs"
        log_dir.mkdir(parents=True)
        (log_dir / "env-sync.log").write_text(
            "2026-05-21T10:00:00Z SUCCESS seed-infisical exit=0\n"
        )

        handler = HandlerHandoffEffect()
        with pytest.raises(HandoffGateError) as exc_info:
            handler.handle(
                session_id="sess-no-target",
                correlation_id=uuid.uuid4(),
                cwd=str(git_repo),
            )

        assert exc_info.value.exit_code == 2
        handoff_dir = state_dir / "session" / "handoff"
        written = (
            list(handoff_dir.glob("handoff-*.yaml")) if handoff_dir.exists() else []
        )
        assert written == [], f"Expected no artifact, found: {written}"


@pytest.mark.unit
class TestStderrOutput:
    """HandoffGateError.__init__ writes to stderr — callers can capture and log it."""

    def test_gate_error_writes_to_stderr(self, valid_log: Path) -> None:
        fake_stderr = io.StringIO()
        with (
            patch(
                _PATCH_SSH_PROBE,
                return_value=("", "timed out after 5s"),
            ),
            patch("sys.stderr", fake_stderr),
            pytest.raises(HandoffGateError),
        ):
            validate_infra_sources(
                env_sync_log_path=valid_log,
                ssh_target=_SSH_TARGET,
            )
        written = fake_stderr.getvalue()
        assert "HANDOFF_GATE_FAILURE" in written

    def test_gate_error_stderr_names_failing_source(self, valid_log: Path) -> None:
        def _postgres_fails(
            ssh_target: str, remote_cmd: str, timeout_s: int
        ) -> tuple[str, str | None]:
            if "postgres" in remote_cmd:
                return "", "exit 1: container not found"
            return _healthy_probe_mock(ssh_target, remote_cmd, timeout_s)

        fake_stderr = io.StringIO()
        with (
            patch(_PATCH_SSH_PROBE, side_effect=_postgres_fails),
            patch("sys.stderr", fake_stderr),
            pytest.raises(HandoffGateError),
        ):
            validate_infra_sources(
                env_sync_log_path=valid_log,
                ssh_target=_SSH_TARGET,
            )
        written = fake_stderr.getvalue()
        assert "postgres" in written


@pytest.mark.unit
class TestEnvSyncLogNoSuccessLine:
    """env-sync.log present but no SUCCESS line → artifact reflects seed-infisical last SUCCESS: NEVER."""

    def test_log_with_only_failures_marks_success_never(self, tmp_path: Path) -> None:
        log = tmp_path / "env-sync.log"
        log.write_text(
            "2026-05-21T08:00:00Z FAILURE seed-infisical exit=1\n"
            "2026-05-21T09:00:00Z FAILURE seed-infisical exit=1\n"
        )
        result = _parse_env_sync_log(log)
        assert result["seed_infisical_last_success"] == "NEVER"

    def test_log_with_no_success_written_to_artifact(
        self,
        git_repo: Path,
        state_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Gate passes (log exists), but artifact reflects NEVER for last SUCCESS."""
        import yaml

        monkeypatch.setenv("ONEX_STATE_DIR", str(state_dir))
        monkeypatch.setenv("ONEX_INFRA_SSH_TARGET", _SSH_TARGET)

        log_dir = state_dir / "logs"
        log_dir.mkdir(parents=True)
        (log_dir / "env-sync.log").write_text(
            "2026-05-21T08:00:00Z FAILURE seed-infisical exit=1\n"
            "2026-05-21T09:00:00Z FAILURE seed-infisical exit=1\n"
        )

        with (
            patch(_PATCH_SSH_PROBE, side_effect=_healthy_probe_mock),
            patch(_PATCH_SSH, side_effect=_mock_ssh_ok),
        ):
            handler = HandlerHandoffEffect()
            result = handler.handle(
                session_id="sess-nosuccess",
                correlation_id=uuid.uuid4(),
                cwd=str(git_repo),
            )

        data = yaml.safe_load(Path(result["artifact_path"]).read_text())
        assert data["infra_health"]["seed_infisical_last_success"] == "NEVER"


@pytest.mark.unit
class TestPerServiceProbeFailure:
    """Each service probe failure in isolation triggers gate block and names the service."""

    @pytest.mark.parametrize("failing_service", list(_ALL_SERVICES))
    def test_single_service_failure_blocks_gate(
        self,
        failing_service: str,
        valid_log: Path,
    ) -> None:
        from omnimarket.nodes.node_handoff_effect.service_catalog import _SERVICE_PROBES

        # Build name→remote_cmd lookup so services whose name differs from their
        # docker filter keyword (e.g. "kafka" uses "redpanda" in the cmd) still match.
        name_to_cmd = {name: cmd for name, cmd, _, _ in _SERVICE_PROBES}
        failing_cmd = name_to_cmd[failing_service]

        def _one_fails(
            ssh_target: str, remote_cmd: str, timeout_s: int
        ) -> tuple[str, str | None]:
            if remote_cmd == failing_cmd:
                return "", f"exit 1: {failing_service} unavailable"
            return _healthy_probe_mock(ssh_target, remote_cmd, timeout_s)

        with (
            patch(_PATCH_SSH_PROBE, side_effect=_one_fails),
            pytest.raises(HandoffGateError) as exc_info,
        ):
            validate_infra_sources(
                env_sync_log_path=valid_log,
                ssh_target=_SSH_TARGET,
            )
        assert exc_info.value.exit_code == 2
        assert failing_service in str(exc_info.value)


@pytest.mark.unit
class TestDockerServiceUnknownOutput:
    """A service that returns empty docker ps output is reported as UNKNOWN (unhealthy)."""

    def test_empty_docker_ps_marks_service_unhealthy(self) -> None:
        """docker ps returns empty string → service not running → healthy=False."""
        with patch(_PATCH_SSH_PROBE, return_value=("", None)):
            snapshot = probe_services(
                ssh_target=_SSH_TARGET,
                timeout_s=5,
                service_probes=(
                    (
                        "postgres",
                        "docker ps --filter name=postgres --format '{{.Names}}|{{.Status}}'",
                        "Up",
                        False,
                    ),
                ),
            )
        result = snapshot.get("postgres")
        assert result is not None
        assert result.healthy is False

    def test_empty_docker_ps_gate_blocks(self, valid_log: Path) -> None:
        """Gate treats empty docker ps output as probe failure and blocks handoff."""
        with (
            patch(_PATCH_SSH_PROBE, return_value=("", None)),
            pytest.raises(HandoffGateError) as exc_info,
        ):
            validate_infra_sources(
                env_sync_log_path=valid_log,
                ssh_target=_SSH_TARGET,
            )
        assert exc_info.value.exit_code == 2


@pytest.mark.unit
class TestServiceCatalogSnapshot:
    """ServiceCatalogSnapshot.is_fully_healthy() reflects probe results correctly."""

    def test_all_healthy_snapshot(self) -> None:
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        snapshot = ServiceCatalogSnapshot(
            snapshot_timestamp=now,
            services=tuple(
                ServiceProbeResult(
                    name=name,
                    status="ok",
                    healthy=True,
                    probe_timestamp=now,
                )
                for name in _ALL_SERVICES
            ),
        )
        assert snapshot.is_fully_healthy() is True

    def test_one_unhealthy_snapshot(self) -> None:
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        services = [
            ServiceProbeResult(
                name=name,
                status="ok",
                healthy=(name != "postgres"),
                probe_timestamp=now,
            )
            for name in _ALL_SERVICES
        ]
        snapshot = ServiceCatalogSnapshot(
            snapshot_timestamp=now,
            services=tuple(services),
        )
        assert snapshot.is_fully_healthy() is False
        assert snapshot.get("postgres") is not None
        assert snapshot.get("postgres").healthy is False  # type: ignore[union-attr]


@pytest.mark.integration
class TestIntegrationUnreachableHost:
    """Integration test using a known-bad SSH target to verify real failure paths.

    Tag: @pytest.mark.integration
    These tests do not block merge when .201 is unavailable — they simply
    exercise the real subprocess code path without mocks.
    """

    _BAD_TARGET = "root@127.0.0.2"  # RFC 5737 / loopback — always connection refused

    def test_unreachable_host_returns_error_not_exception(self) -> None:
        """_run_ssh_probe handles unreachable host gracefully — no unhandled exception."""
        output, error = _run_ssh_probe(
            ssh_target=self._BAD_TARGET,
            remote_cmd="echo hi",
            timeout_s=2,
        )
        assert output == ""
        assert error is not None
        assert len(error) > 0

    def test_unreachable_host_gate_blocks(self, valid_log: Path) -> None:
        """validate_infra_sources with unreachable host raises HandoffGateError."""
        with pytest.raises(HandoffGateError) as exc_info:
            validate_infra_sources(
                env_sync_log_path=valid_log,
                ssh_target=self._BAD_TARGET,
                ssh_timeout_s=2,
            )
        assert exc_info.value.exit_code == 2
        assert "HANDOFF_GATE_FAILURE" in str(exc_info.value)

    def test_unreachable_host_no_artifact_written(
        self,
        git_repo: Path,
        state_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """handler.handle() with unreachable host writes no artifact file."""
        monkeypatch.setenv("ONEX_STATE_DIR", str(state_dir))
        monkeypatch.setenv("ONEX_INFRA_SSH_TARGET", self._BAD_TARGET)
        log_dir = state_dir / "logs"
        log_dir.mkdir(parents=True)
        (log_dir / "env-sync.log").write_text(
            "2026-05-21T10:00:00Z SUCCESS seed-infisical exit=0\n"
        )

        handler = HandlerHandoffEffect()
        with pytest.raises(HandoffGateError):
            handler.handle(
                session_id="sess-bad-host",
                correlation_id=uuid.uuid4(),
                cwd=str(git_repo),
            )

        handoff_dir = state_dir / "session" / "handoff"
        written = (
            list(handoff_dir.glob("handoff-*.yaml")) if handoff_dir.exists() else []
        )
        assert written == [], f"Expected no artifact, found: {written}"
