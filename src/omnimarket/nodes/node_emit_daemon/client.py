# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Synchronous Unix socket client for the emit daemon.

stdlib-only (socket + json) for maximum portability across all
IDE/coding tool platforms. Zero external dependencies.

Protocol: newline-delimited JSON over Unix domain socket.
    Emit:  {"event_type": "...", "payload": {...}}\\n
    Reply: {"status": "queued", "event_id": "..."}\\n

    Ping:  {"command": "ping"}\\n
    Reply: {"status": "ok", "queue_size": N, "spool_size": N}\\n
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import socket

logger = logging.getLogger(__name__)

# 4 KiB is generous for a single JSON response line
_RECV_BUFSIZE = 4096
# Guard against unbounded buffer growth from a misbehaving daemon (1 MiB)
_MAX_RESPONSE_SIZE = 1_048_576
# Cap read loop iterations to prevent indefinite blocking
_MAX_READ_ITERATIONS = 64


def default_socket_path() -> str:
    """Resolve the default socket path.

    Resolution order: ONEX_EMIT_SOCKET_PATH env > XDG_RUNTIME_DIR > /tmp fallback.
    """
    env_path = os.environ.get("ONEX_EMIT_SOCKET_PATH")
    if env_path:
        return env_path
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return os.path.join(xdg, "onex", "emit.sock")
    return "/tmp/onex-emit.sock"


class EmitClient:
    """Synchronous client for the emit daemon.

    Connection is lazy (opened on first call) and auto-reconnects on
    broken pipe. Thread-safety is the caller's responsibility.

    Args:
        socket_path: Path to the daemon's Unix domain socket.
            If None, uses default_socket_path() resolution.
        timeout: Socket timeout in seconds for connect + send + recv.
    """

    def __init__(
        self,
        socket_path: str | None = None,
        timeout: float = 5.0,
    ) -> None:
        self._socket_path = socket_path or default_socket_path()
        self._timeout = timeout
        self._sock: socket.socket | None = None
        self._buf = bytearray()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _connect(self) -> socket.socket:
        """Return an open socket, connecting lazily if needed."""
        if self._sock is not None:
            return self._sock
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(self._timeout)
            sock.connect(self._socket_path)
        except Exception:
            sock.close()
            raise
        self._sock = sock
        self._buf = bytearray()
        return sock

    def _send_and_recv(self, request: dict[str, object]) -> dict[str, object]:
        """Send a request and read the response, reconnecting once on failure."""
        line = json.dumps(request).encode("utf-8") + b"\n"
        try:
            sock = self._connect()
            sock.sendall(line)
            return self._read_response(sock)
        except OSError:
            self.close()
            sock = self._connect()
            sock.sendall(line)
            return self._read_response(sock)

    def _read_response(self, sock: socket.socket) -> dict[str, object]:
        """Read until newline and parse JSON, preserving leftover bytes."""
        iterations = 0
        while b"\n" not in self._buf:
            chunk = sock.recv(_RECV_BUFSIZE)
            if not chunk:
                raise ConnectionResetError("daemon closed connection")
            self._buf.extend(chunk)
            iterations += 1
            if len(self._buf) > _MAX_RESPONSE_SIZE:
                raise ValueError("daemon response exceeded size limit")
            if iterations >= _MAX_READ_ITERATIONS:
                raise ValueError("daemon response exceeded read iteration limit")
        idx = self._buf.index(b"\n")
        resp_line = self._buf[:idx]
        self._buf = self._buf[idx + 1 :]
        return json.loads(resp_line)  # type: ignore[no-any-return]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def emit_sync(self, event_type: str, payload: dict[str, object]) -> str:
        """Emit an event to the daemon synchronously.

        Returns:
            The event_id assigned by the daemon.

        Raises:
            ValueError: If the daemon returns an error response.
            ConnectionRefusedError: If the daemon is not running.
            OSError: On socket-level failures.
        """
        resp = self._send_and_recv({"event_type": event_type, "payload": payload})
        if resp.get("status") == "queued":
            return str(resp["event_id"])
        reason = resp.get("reason", "unknown error")
        raise ValueError(f"Daemon rejected event: {reason}")

    def health_sync(self) -> dict[str, object]:
        """Get detailed health from the daemon.

        Returns:
            Health snapshot dict with circuit_state, counters, timestamps.

        Raises:
            ConnectionRefusedError: If the daemon is not running.
            OSError: On socket-level failures.
        """
        return self._send_and_recv({"command": "health"})

    def is_daemon_running_sync(self) -> bool:
        """Ping the daemon. Returns True if it responds OK."""
        try:
            resp = self._send_and_recv({"command": "ping"})
            return resp.get("status") == "ok"
        except Exception:
            return False

    def close(self) -> None:
        """Close the socket connection."""
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()
            self._sock = None
            self._buf = bytearray()

    def __enter__(self) -> EmitClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __del__(self) -> None:
        """Best-effort cleanup of open socket on garbage collection."""
        with contextlib.suppress(Exception):
            self.close()


__all__ = ["EmitClient", "default_socket_path"]
