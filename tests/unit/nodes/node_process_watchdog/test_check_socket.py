"""Unit tests for SocketCheckTarget — real stat()-based socket health check.

Tests use tmp_path fixture only; no real filesystem state required.

Note: AF_UNIX paths are capped at 108 chars on macOS/Linux. We use
tempfile.mkdtemp() under /tmp to ensure the path stays short enough.
"""

from __future__ import annotations

import os
import shutil
import socket as sock_mod
import tempfile
import time

import pytest

from omnimarket.nodes.node_process_watchdog.handlers.check_socket import (
    SocketCheckTarget,
)
from omnimarket.nodes.node_process_watchdog.models.model_watchdog_state import (
    EnumCheckStatus,
)


def _make_short_sock(name: str) -> tuple[str, str]:
    """Create a socket file under the system temp dir; return (dir, sock_path).

    Uses tempfile.gettempdir() rather than a hardcoded /tmp so the path stays
    within the 108-char AF_UNIX limit on macOS/Linux across CI environments.
    """
    d = tempfile.mkdtemp(dir=tempfile.gettempdir())
    sock_path = os.path.join(d, name)
    s = sock_mod.socket(sock_mod.AF_UNIX, sock_mod.SOCK_STREAM)
    s.bind(sock_path)
    s.close()
    return d, sock_path


@pytest.mark.unit
def test_socket_check_target_fail_when_missing(
    tmp_path: pytest.TempPathFactory,
) -> None:
    """DOWN when socket path does not exist."""
    nonexistent = tmp_path / "missing.sock"
    target = SocketCheckTarget(socket_path=str(nonexistent), stale_after_seconds=300)
    result = target.check()
    assert result.status == EnumCheckStatus.DOWN
    assert "does not exist" in result.message.lower()


@pytest.mark.unit
def test_socket_check_target_warn_when_stale() -> None:
    """DEGRADED when socket exists but mtime older than stale_after_seconds."""
    d, sock_path = _make_short_sock("stale.sock")
    try:
        old = time.time() - 600
        os.utime(sock_path, (old, old))
        target = SocketCheckTarget(socket_path=sock_path, stale_after_seconds=300)
        result = target.check()
        assert result.status == EnumCheckStatus.DEGRADED
        assert "stale" in result.message.lower()
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.mark.unit
def test_socket_check_target_pass_when_fresh() -> None:
    """HEALTHY when socket exists and mtime is within threshold."""
    d, sock_path = _make_short_sock("fresh.sock")
    try:
        target = SocketCheckTarget(socket_path=sock_path, stale_after_seconds=300)
        result = target.check()
        assert result.status == EnumCheckStatus.HEALTHY
    finally:
        shutil.rmtree(d, ignore_errors=True)
