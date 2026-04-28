"""Unit tests for RpkCheckTarget — mocked subprocess, no rpk binary needed."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from omnimarket.nodes.node_process_watchdog.models.model_watchdog_state import (
    EnumCheckStatus,
    EnumCheckTarget,
)
from omnimarket.nodes.node_process_watchdog.targets.rpk_check_target import (
    RpkCheckTarget,
)


def _target(**overrides: object) -> RpkCheckTarget:
    defaults = {
        "consumer_group": "test-consumers",
        "timeout": 10.0,
        "category": EnumCheckTarget.KAFKA_CONSUMERS,
    }
    defaults.update(overrides)
    return RpkCheckTarget(**defaults)


def _completed_process(
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["rpk", "group", "describe", "test-consumers"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


@pytest.mark.unit
class TestRpkCheckTargetValidation:
    def test_rejects_empty_consumer_group(self) -> None:
        with pytest.raises(ValueError, match="consumer_group"):
            _target(consumer_group="")

    def test_rejects_zero_timeout(self) -> None:
        with pytest.raises(ValueError, match="timeout"):
            _target(timeout=0)

    def test_rejects_negative_timeout(self) -> None:
        with pytest.raises(ValueError, match="timeout"):
            _target(timeout=-1)


@pytest.mark.unit
class TestRpkCheckTargetCheck:
    @patch(
        "omnimarket.nodes.node_process_watchdog.targets.rpk_check_target.subprocess.run"
    )
    def test_healthy_when_group_has_members(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _completed_process(
            stdout='{"group": "test-consumers", "members": [{"id": "consumer-1"}]}'
        )

        result = _target().check()
        assert result.status == EnumCheckStatus.HEALTHY
        assert result.category == EnumCheckTarget.KAFKA_CONSUMERS

    @patch(
        "omnimarket.nodes.node_process_watchdog.targets.rpk_check_target.subprocess.run"
    )
    def test_down_when_zero_members(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _completed_process(
            stdout='{"group": "test-consumers", "members":[]}'
        )

        result = _target().check()
        assert result.status == EnumCheckStatus.DOWN
        assert "0 members" in result.message

    @patch(
        "omnimarket.nodes.node_process_watchdog.targets.rpk_check_target.subprocess.run"
    )
    def test_down_when_zero_members_spaced(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _completed_process(
            stdout='{"group": "test-consumers", "members": []}'
        )

        result = _target().check()
        assert result.status == EnumCheckStatus.DOWN

    @patch(
        "omnimarket.nodes.node_process_watchdog.targets.rpk_check_target.subprocess.run"
    )
    def test_down_when_rpk_nonzero_exit(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _completed_process(
            returncode=1, stderr="error: unknown group"
        )

        result = _target().check()
        assert result.status == EnumCheckStatus.DOWN
        assert "rpk failed" in result.message.lower()

    @patch(
        "omnimarket.nodes.node_process_watchdog.targets.rpk_check_target.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="rpk", timeout=10),
    )
    def test_unknown_on_timeout(self, mock_run: MagicMock) -> None:
        result = _target().check()
        assert result.status == EnumCheckStatus.UNKNOWN
        assert "timed out" in result.message.lower()

    @patch(
        "omnimarket.nodes.node_process_watchdog.targets.rpk_check_target.subprocess.run",
        side_effect=FileNotFoundError("rpk not found"),
    )
    def test_unknown_on_missing_binary(self, mock_run: MagicMock) -> None:
        result = _target().check()
        assert result.status == EnumCheckStatus.UNKNOWN
        assert "not found" in result.message.lower()

    @patch(
        "omnimarket.nodes.node_process_watchdog.targets.rpk_check_target.subprocess.run",
        side_effect=RuntimeError("unexpected"),
    )
    def test_unknown_on_unexpected_error(self, mock_run: MagicMock) -> None:
        result = _target().check()
        assert result.status == EnumCheckStatus.UNKNOWN

    def test_restart_returns_false(self) -> None:
        assert _target().restart() is False
