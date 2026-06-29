# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Protocol and error-branch unit tests for EmitClient (OMN-13741).

Covers branches left uncovered by test_emit_client_rehome.py:
- _connect(): sock.connect() raises → sock.close() + reraise (lines 88-90)
- _send_and_recv(): first-attempt OSError → close + reconnect + retry (lines 102-106)
- _read_response(): empty recv → ConnectionResetError (line 114)
- _read_response(): buffer > 1 MiB → ValueError (line 118)
- _read_response(): 64 iterations reached → ValueError (line 120)
- is_daemon_running_sync(): any exception → False (lines 164-165)
- close(): sock.close() raises OSError, suppressed (lines 176-179)
- context manager __enter__/__exit__
- close() idempotency (double-call)
- happy-path emit / health / ping via socket.socketpair() — no live daemon

Strategy: socket.socketpair() for real protocol I/O; mock.patch / mock.patch.object
for error injection.  Zero external dependencies; stdlib-only.
"""

from __future__ import annotations

import json
import socket
from unittest import mock

import pytest

from omnimarket.events.emit_client import (
    _MAX_READ_ITERATIONS,  # type: ignore[attr-defined]
    _MAX_RESPONSE_SIZE,  # type: ignore[attr-defined]
    EmitClient,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _socketpair_client(response: dict[str, object]) -> tuple[EmitClient, socket.socket]:
    """Return *(client, server_sock)* wired by a real Unix socket pair.

    The server_sock already has *response* written into the wire buffer.
    The caller is responsible for closing both the returned *server_sock* and
    the *client* after the test.
    """
    server_sock, client_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    raw = json.dumps(response).encode("utf-8") + b"\n"
    server_sock.sendall(raw)

    client = EmitClient(socket_path="/dev/null", timeout=2.0)
    # Inject the already-connected socket to bypass the real UNIX connect().
    client._sock = client_sock  # type: ignore[attr-defined]
    return client, server_sock


# ---------------------------------------------------------------------------
# _connect()
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestConnect:
    """_connect() closes the raw socket and re-raises on connection failure."""

    def test_connect_failure_closes_sock_and_reraises(self) -> None:
        client = EmitClient(socket_path="/nonexistent-omn13741.sock", timeout=0.1)
        mock_sock = mock.MagicMock()
        mock_sock.connect.side_effect = OSError("connection refused")

        with (
            mock.patch("socket.socket", return_value=mock_sock),
            pytest.raises(OSError, match="connection refused"),
        ):
            client._connect()  # type: ignore[attr-defined]

        mock_sock.close.assert_called_once()
        assert client._sock is None  # type: ignore[attr-defined]

    def test_connect_reuses_existing_socket(self) -> None:
        """When _sock is already set, _connect() returns it without creating a new one."""
        client = EmitClient(socket_path="/fake.sock")
        sentinel = mock.MagicMock()
        client._sock = sentinel  # type: ignore[attr-defined]

        with mock.patch("socket.socket") as mock_cls:
            result = client._connect()  # type: ignore[attr-defined]

        assert result is sentinel
        mock_cls.assert_not_called()


# ---------------------------------------------------------------------------
# _send_and_recv()
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSendAndRecvRetry:
    """First-attempt OSError triggers close + reconnect + retry path."""

    def test_retry_on_os_error_from_first_sendall(self) -> None:
        client = EmitClient(socket_path="/fake.sock", timeout=1.0)
        broken_sock = mock.MagicMock()
        broken_sock.sendall.side_effect = OSError("broken pipe")
        good_sock = mock.MagicMock()
        expected: dict[str, object] = {"status": "queued", "event_id": "retry-evt-id"}
        sockets = iter([broken_sock, good_sock])

        def connect_mock() -> mock.MagicMock:
            sock = next(sockets)
            client._sock = sock  # type: ignore[attr-defined]
            return sock

        with (
            mock.patch.object(client, "close", wraps=client.close) as close_mock,
            mock.patch.object(client, "_connect", side_effect=connect_mock),
            mock.patch.object(client, "_read_response", return_value=expected),
        ):
            result = client._send_and_recv(  # type: ignore[attr-defined]
                {"event_type": "x.retry", "payload": {}}
            )

        assert result == expected
        # Both sockets had sendall called
        broken_sock.sendall.assert_called_once()
        good_sock.sendall.assert_called_once()
        close_mock.assert_called_once()
        broken_sock.close.assert_called_once()

    def test_retry_propagates_second_os_error(self) -> None:
        """If the retry also raises, the error is not caught again."""
        client = EmitClient(socket_path="/fake.sock", timeout=1.0)
        broken_sock1 = mock.MagicMock()
        broken_sock1.sendall.side_effect = OSError("first failure")
        broken_sock2 = mock.MagicMock()
        broken_sock2.sendall.side_effect = OSError("second failure")
        sockets = iter([broken_sock1, broken_sock2])

        def connect_mock() -> mock.MagicMock:
            sock = next(sockets)
            client._sock = sock  # type: ignore[attr-defined]
            return sock

        with (
            mock.patch.object(client, "close", wraps=client.close) as close_mock,
            mock.patch.object(client, "_connect", side_effect=connect_mock),
            pytest.raises(OSError, match="second failure"),
        ):
            client._send_and_recv({"event_type": "x", "payload": {}})  # type: ignore[attr-defined]

        close_mock.assert_called_once()
        broken_sock1.close.assert_called_once()


# ---------------------------------------------------------------------------
# _read_response()
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestReadResponseErrors:
    """_read_response() raises on empty recv, buffer overflow, and iteration limit."""

    def test_empty_recv_raises_connection_reset_error(self) -> None:
        client = EmitClient(socket_path="/fake.sock")
        mock_sock = mock.MagicMock()
        mock_sock.recv.return_value = b""

        with pytest.raises(ConnectionResetError, match="daemon closed connection"):
            client._read_response(mock_sock)  # type: ignore[attr-defined]

    def test_buffer_overflow_raises_value_error(self) -> None:
        """A single recv returning > _MAX_RESPONSE_SIZE bytes triggers the guard."""
        client = EmitClient(socket_path="/fake.sock")
        mock_sock = mock.MagicMock()
        # The mock ignores the bufsize argument and returns an oversized chunk.
        mock_sock.recv.return_value = b"x" * (_MAX_RESPONSE_SIZE + 1)

        with pytest.raises(ValueError, match="exceeded size limit"):
            client._read_response(mock_sock)  # type: ignore[attr-defined]

    def test_iteration_limit_raises_value_error(self) -> None:
        """After _MAX_READ_ITERATIONS calls without a newline, raises ValueError."""
        client = EmitClient(socket_path="/fake.sock")
        mock_sock = mock.MagicMock()
        # Return one byte per call (no newline) — 64 * 1 byte = 64 bytes << 1 MiB,
        # so the size guard never fires first.
        mock_sock.recv.return_value = b"x"

        with pytest.raises(ValueError, match="exceeded read iteration limit"):
            client._read_response(mock_sock)  # type: ignore[attr-defined]

        assert mock_sock.recv.call_count == _MAX_READ_ITERATIONS

    def test_leftover_bytes_preserved_between_calls(self) -> None:
        """Bytes after the first newline are kept in _buf for the next call."""
        server_sock, client_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        first = json.dumps({"id": "A"}).encode() + b"\n"
        second = json.dumps({"id": "B"}).encode() + b"\n"
        server_sock.sendall(first + second)
        server_sock.close()

        client = EmitClient(socket_path="/dev/null", timeout=2.0)
        client._sock = client_sock  # type: ignore[attr-defined]
        try:
            r1 = client._read_response(client_sock)  # type: ignore[attr-defined]
            r2 = client._read_response(client_sock)  # type: ignore[attr-defined]
        finally:
            client.close()

        assert r1["id"] == "A"
        assert r2["id"] == "B"


# ---------------------------------------------------------------------------
# is_daemon_running_sync()
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestIsDaemonRunningSync:
    """is_daemon_running_sync() returns False for any exception, True on ok."""

    def test_returns_false_on_connection_refused(self) -> None:
        client = EmitClient(socket_path="/nonexistent-omn13741.sock", timeout=0.1)
        with mock.patch.object(
            client, "_send_and_recv", side_effect=ConnectionRefusedError("refused")
        ):
            assert client.is_daemon_running_sync() is False

    def test_returns_false_on_os_error(self) -> None:
        client = EmitClient(socket_path="/fake.sock")
        with mock.patch.object(
            client, "_send_and_recv", side_effect=OSError("io error")
        ):
            assert client.is_daemon_running_sync() is False

    def test_returns_false_on_generic_exception(self) -> None:
        client = EmitClient(socket_path="/fake.sock")
        with mock.patch.object(
            client, "_send_and_recv", side_effect=RuntimeError("boom")
        ):
            assert client.is_daemon_running_sync() is False

    def test_returns_true_when_status_ok(self) -> None:
        client = EmitClient(socket_path="/fake.sock")
        with mock.patch.object(
            client,
            "_send_and_recv",
            return_value={"status": "ok", "queue_size": 0, "spool_size": 0},
        ):
            assert client.is_daemon_running_sync() is True

    def test_returns_false_when_status_not_ok(self) -> None:
        client = EmitClient(socket_path="/fake.sock")
        with mock.patch.object(
            client, "_send_and_recv", return_value={"status": "degraded"}
        ):
            assert client.is_daemon_running_sync() is False


# ---------------------------------------------------------------------------
# close()
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestClose:
    """close() suppresses OSError from sock.close() and is idempotent."""

    def test_close_suppresses_os_error(self) -> None:
        client = EmitClient(socket_path="/fake.sock")
        bad_sock = mock.MagicMock()
        bad_sock.close.side_effect = OSError("bad file descriptor")
        client._sock = bad_sock  # type: ignore[attr-defined]

        # Must not raise despite sock.close() raising OSError.
        client.close()

        assert client._sock is None  # type: ignore[attr-defined]
        bad_sock.close.assert_called_once()

    def test_close_is_idempotent(self) -> None:
        """Calling close() twice must not raise and must not double-close the sock."""
        client = EmitClient(socket_path="/fake.sock")
        mock_sock = mock.MagicMock()
        client._sock = mock_sock  # type: ignore[attr-defined]

        client.close()
        client.close()  # second call: _sock is already None → no-op

        mock_sock.close.assert_called_once()
        assert client._sock is None  # type: ignore[attr-defined]

    def test_close_with_no_sock_is_noop(self) -> None:
        client = EmitClient(socket_path="/fake.sock")
        assert client._sock is None  # type: ignore[attr-defined]
        client.close()  # must not raise

    def test_close_resets_buf(self) -> None:
        client = EmitClient(socket_path="/fake.sock")
        mock_sock = mock.MagicMock()
        client._sock = mock_sock  # type: ignore[attr-defined]
        client._buf = bytearray(b"leftover")  # type: ignore[attr-defined]

        client.close()

        assert client._buf == bytearray()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestContextManager:
    """EmitClient.__enter__ / __exit__ delegate to close()."""

    def test_enter_returns_self(self) -> None:
        client = EmitClient(socket_path="/fake.sock")
        assert client.__enter__() is client

    def test_exit_closes_socket(self) -> None:
        client = EmitClient(socket_path="/fake.sock")
        mock_sock = mock.MagicMock()
        client._sock = mock_sock  # type: ignore[attr-defined]

        client.__exit__(None, None, None)

        mock_sock.close.assert_called_once()
        assert client._sock is None  # type: ignore[attr-defined]

    def test_context_manager_closes_on_normal_exit(self) -> None:
        mock_sock = mock.MagicMock()
        with EmitClient(socket_path="/fake.sock") as client:
            client._sock = mock_sock  # type: ignore[attr-defined]

        mock_sock.close.assert_called_once()
        assert client._sock is None  # type: ignore[attr-defined]

    def test_context_manager_closes_on_exception(self) -> None:
        mock_sock = mock.MagicMock()

        def _raise_inside_ctx() -> None:
            with EmitClient(socket_path="/fake.sock") as client:
                client._sock = mock_sock  # type: ignore[attr-defined]
                raise ValueError("test exception")

        with pytest.raises(ValueError, match="test exception"):
            _raise_inside_ctx()

        mock_sock.close.assert_called_once()


# ---------------------------------------------------------------------------
# Happy-path protocol via socket.socketpair()
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHappyPathProtocol:
    """Full protocol round-trips using real Unix socket pairs — no live daemon."""

    def test_emit_sync_returns_event_id(self) -> None:
        client, server_sock = _socketpair_client(
            {"status": "queued", "event_id": "evt-happy-001"}
        )
        try:
            result = client.emit_sync("onex.test.event", {"key": "value"})
        finally:
            server_sock.close()
            client.close()

        assert result == "evt-happy-001"

    def test_emit_sync_raises_on_daemon_error_response(self) -> None:
        client, server_sock = _socketpair_client(
            {"status": "error", "reason": "queue at capacity"}
        )
        try:
            with pytest.raises(ValueError, match="queue at capacity"):
                client.emit_sync("onex.test.event", {})
        finally:
            server_sock.close()
            client.close()

    def test_emit_sync_unknown_reason_fallback(self) -> None:
        """When 'reason' is absent, the error message is 'unknown error'."""
        client, server_sock = _socketpair_client({"status": "error"})
        try:
            with pytest.raises(ValueError, match="unknown error"):
                client.emit_sync("onex.test.event", {})
        finally:
            server_sock.close()
            client.close()

    def test_health_sync_returns_full_dict(self) -> None:
        expected: dict[str, object] = {
            "status": "ok",
            "queue_size": 7,
            "spool_size": 3,
            "circuit_state": "closed",
        }
        client, server_sock = _socketpair_client(expected)
        try:
            result = client.health_sync()
        finally:
            server_sock.close()
            client.close()

        assert result == expected

    def test_ping_is_detected_as_running(self) -> None:
        client, server_sock = _socketpair_client(
            {"status": "ok", "queue_size": 0, "spool_size": 0}
        )
        try:
            running = client.is_daemon_running_sync()
        finally:
            server_sock.close()
            client.close()

        assert running is True

    def test_emit_sync_sends_correct_wire_format(self) -> None:
        """The request written to the wire matches the expected newline-delimited JSON."""
        server_sock, client_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        # Pre-queue the daemon response so _read_response doesn't block.
        server_sock.sendall(
            json.dumps({"status": "queued", "event_id": "wire-check"}).encode() + b"\n"
        )

        client = EmitClient(socket_path="/dev/null", timeout=2.0)
        client._sock = client_sock  # type: ignore[attr-defined]
        try:
            client.emit_sync("test.wire", {"a": 1})
        finally:
            client.close()

        # Read what the client sent to the server side.
        server_sock.settimeout(1.0)
        received = server_sock.recv(4096)
        server_sock.close()

        decoded = json.loads(received.rstrip(b"\n"))
        assert decoded == {"event_type": "test.wire", "payload": {"a": 1}}
