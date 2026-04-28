"""Unit tests for SocketConnectCheckTarget — mocked socket, no network access."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from omnimarket.nodes.node_process_watchdog.models.model_watchdog_state import (
    EnumCheckStatus,
    EnumCheckTarget,
)
from omnimarket.nodes.node_process_watchdog.targets.socket_connect_check_target import (
    SocketConnectCheckTarget,
)


def _target(**overrides: object) -> SocketConnectCheckTarget:
    defaults = {
        "name": "test-emit-daemon",
        "host": "127.0.0.1",
        "port": 9877,
        "timeout": 5.0,
        "category": EnumCheckTarget.EMIT_DAEMON,
    }
    defaults.update(overrides)
    return SocketConnectCheckTarget(**defaults)


@pytest.mark.unit
class TestSocketConnectCheckTargetValidation:
    def test_rejects_empty_name(self) -> None:
        with pytest.raises(ValueError, match="name"):
            _target(name="")

    def test_rejects_empty_host(self) -> None:
        with pytest.raises(ValueError, match="host"):
            _target(host="")

    def test_rejects_zero_port(self) -> None:
        with pytest.raises(ValueError, match="port"):
            _target(port=0)

    def test_rejects_port_over_65535(self) -> None:
        with pytest.raises(ValueError, match="port"):
            _target(port=70000)

    def test_rejects_negative_timeout(self) -> None:
        with pytest.raises(ValueError, match="timeout"):
            _target(timeout=-1)


@pytest.mark.unit
class TestSocketConnectCheckTargetCheck:
    @patch(
        "omnimarket.nodes.node_process_watchdog.targets.socket_connect_check_target.socket.socket"
    )
    def test_healthy_on_connect(self, mock_socket_cls: MagicMock) -> None:
        mock_sock = MagicMock()
        mock_socket_cls.return_value = mock_sock

        result = _target().check()
        assert result.status == EnumCheckStatus.HEALTHY
        assert result.category == EnumCheckTarget.EMIT_DAEMON
        mock_sock.connect.assert_called_once_with(("127.0.0.1", 9877))
        mock_sock.close.assert_called_once()

    @patch(
        "omnimarket.nodes.node_process_watchdog.targets.socket_connect_check_target.socket.socket",
        side_effect=ConnectionRefusedError("refused"),
    )
    def test_down_on_connection_refused(self, mock_socket_cls: MagicMock) -> None:
        result = _target().check()
        assert result.status == EnumCheckStatus.DOWN
        assert "refused" in result.message.lower()

    @patch(
        "omnimarket.nodes.node_process_watchdog.targets.socket_connect_check_target.socket.socket",
        side_effect=TimeoutError("timed out"),
    )
    def test_down_on_timeout(self, mock_socket_cls: MagicMock) -> None:
        result = _target().check()
        assert result.status == EnumCheckStatus.DOWN
        assert "timed out" in result.message.lower()

    @patch(
        "omnimarket.nodes.node_process_watchdog.targets.socket_connect_check_target.socket.socket",
        side_effect=OSError("host unreachable"),
    )
    def test_down_on_os_error(self, mock_socket_cls: MagicMock) -> None:
        result = _target().check()
        assert result.status == EnumCheckStatus.DOWN

    @patch(
        "omnimarket.nodes.node_process_watchdog.targets.socket_connect_check_target.socket.socket",
        side_effect=RuntimeError("unexpected"),
    )
    def test_unknown_on_unexpected_error(self, mock_socket_cls: MagicMock) -> None:
        result = _target().check()
        assert result.status == EnumCheckStatus.UNKNOWN

    def test_restart_returns_false(self) -> None:
        assert _target().restart() is False
