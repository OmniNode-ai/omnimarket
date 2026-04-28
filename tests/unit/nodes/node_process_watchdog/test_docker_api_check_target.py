"""Unit tests for TargetDockerApi — mocked Docker SDK, no daemon access."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from omnimarket.nodes.node_process_watchdog.models.model_watchdog_state import (
    EnumCheckStatus,
    EnumCheckTarget,
)
from omnimarket.nodes.node_process_watchdog.targets.docker_api_check_target import (
    TargetDockerApi,
)


def _target(**overrides: object) -> TargetDockerApi:
    defaults = {
        "container_name": "test-container",
        "category": EnumCheckTarget.DOCKER_CONTAINERS,
    }
    defaults.update(overrides)
    return TargetDockerApi(**defaults)


def _mock_container(status: str, health_status: str | None = None) -> MagicMock:
    state = {"Status": status}
    if health_status is not None:
        state["Health"] = {"Status": health_status}
    container = MagicMock()
    container.attrs = {"State": state}
    return container


@pytest.mark.unit
class TestTargetDockerApiValidation:
    def test_rejects_empty_container_name(self) -> None:
        with pytest.raises(ValueError, match="container_name"):
            _target(container_name="")


@pytest.mark.unit
class TestTargetDockerApiCheck:
    @patch("docker.from_env")
    def test_healthy_when_running(self, mock_from_env: MagicMock) -> None:
        mock_container = _mock_container("running", "healthy")
        mock_from_env.return_value.containers.get.return_value = mock_container

        result = _target().check()
        assert result.status == EnumCheckStatus.HEALTHY
        mock_from_env.return_value.close.assert_called_once()

    @patch("docker.from_env")
    def test_healthy_running_no_health_config(self, mock_from_env: MagicMock) -> None:
        mock_container = _mock_container("running", None)
        mock_from_env.return_value.containers.get.return_value = mock_container

        result = _target().check()
        assert result.status == EnumCheckStatus.HEALTHY

    @patch("docker.from_env")
    def test_degraded_when_unhealthy(self, mock_from_env: MagicMock) -> None:
        mock_container = _mock_container("running", "unhealthy")
        mock_from_env.return_value.containers.get.return_value = mock_container

        result = _target().check()
        assert result.status == EnumCheckStatus.DEGRADED

    @patch("docker.from_env")
    def test_degraded_when_starting(self, mock_from_env: MagicMock) -> None:
        mock_container = _mock_container("running", "starting")
        mock_from_env.return_value.containers.get.return_value = mock_container

        result = _target().check()
        assert result.status == EnumCheckStatus.DEGRADED

    @patch("docker.from_env")
    def test_down_when_not_running(self, mock_from_env: MagicMock) -> None:
        mock_container = _mock_container("exited")
        mock_from_env.return_value.containers.get.return_value = mock_container

        result = _target().check()
        assert result.status == EnumCheckStatus.DOWN

    @patch("docker.from_env")
    def test_down_when_container_not_found(self, mock_from_env: MagicMock) -> None:
        mock_from_env.return_value.containers.get.side_effect = Exception(
            "No such container: test-container"
        )

        result = _target().check()
        assert result.status == EnumCheckStatus.DOWN
        assert "not found" in result.message.lower()
        mock_from_env.return_value.close.assert_called_once()

    @patch("docker.from_env")
    def test_down_when_daemon_unreachable(self, mock_from_env: MagicMock) -> None:
        mock_from_env.return_value.containers.get.side_effect = Exception(
            "Cannot connect to the Docker daemon"
        )

        result = _target().check()
        assert result.status == EnumCheckStatus.DOWN
        assert "unreachable" in result.message.lower()

    @patch("docker.from_env")
    def test_unknown_on_unexpected_error(self, mock_from_env: MagicMock) -> None:
        mock_from_env.return_value.containers.get.side_effect = RuntimeError(
            "unexpected failure"
        )

        result = _target().check()
        assert result.status == EnumCheckStatus.UNKNOWN

    @patch("docker.from_env")
    def test_restart_succeeds(self, mock_from_env: MagicMock) -> None:
        mock_container = MagicMock()
        mock_from_env.return_value.containers.get.return_value = mock_container

        assert _target().restart() is True
        mock_container.restart.assert_called_once_with(timeout=10)
        mock_from_env.return_value.close.assert_called_once()

    @patch("docker.from_env")
    def test_restart_returns_false_on_failure(self, mock_from_env: MagicMock) -> None:
        mock_from_env.return_value.containers.get.side_effect = Exception("fail")

        assert _target().restart() is False
        mock_from_env.return_value.close.assert_called_once()
