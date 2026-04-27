"""Focused tests for the Codex runtime ingress client."""

from __future__ import annotations

import json
import socket
import threading
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.adapters.codex.runtime_client import (
    LocalRuntimeIngressClient,
    default_socket_path,
    main,
)


class _OneShotSocketServer(threading.Thread):
    def __init__(self, socket_path: Path, response: dict[str, object]) -> None:
        super().__init__(daemon=True)
        self._socket_path = socket_path
        self._response = response
        self.ready = threading.Event()
        self.request_line: bytes | None = None
        self.error: Exception | None = None

    def run(self) -> None:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(str(self._socket_path))
                server.listen(1)
                self.ready.set()
                conn, _ = server.accept()
                with conn:
                    buf = bytearray()
                    while b"\n" not in buf:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        buf.extend(chunk)
                    self.request_line = bytes(buf)
                    conn.sendall(json.dumps(self._response).encode("utf-8") + b"\n")
        except Exception as exc:  # pragma: no cover - surfaced through assertions
            self.error = exc
            self.ready.set()


def _socket_path(name: str) -> Path:
    return Path("/tmp") / f"{name}-{uuid4().hex}.sock"


def test_default_socket_path_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEX_RUNTIME_SOCKET_PATH", "/tmp/custom-runtime.sock")
    assert default_socket_path() == "/tmp/custom-runtime.sock"


def test_default_socket_path_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ONEX_RUNTIME_SOCKET_PATH", raising=False)
    assert default_socket_path() == "/tmp/onex-runtime.sock"


def test_dispatch_sync_round_trip(tmp_path: Path) -> None:
    socket_path = _socket_path("omnimarket-runtime-test")
    response = {
        "ok": True,
        "node_alias": "session_orchestrator",
        "resolved_node_name": "node_session_orchestrator",
        "contract_name": "session_orchestrator",
        "topic": "onex.cmd.omnimarket.session-orchestrator-start.v1",
        "terminal_event": "onex.evt.omnimarket.session-orchestrator-completed.v1",
        "dispatch_result": {"status": "complete", "dispatch_queue": []},
    }
    server = _OneShotSocketServer(socket_path, response)
    server.start()
    assert server.ready.wait(timeout=2.0)

    with LocalRuntimeIngressClient(
        socket_path=str(socket_path), timeout_seconds=2.0
    ) as client:
        result = client.dispatch_sync(
            node_alias="session_orchestrator",
            payload={"dry_run": True},
            timeout_ms=1234,
        )

    server.join(timeout=2.0)
    assert server.error is None
    assert result.ok is True
    assert result.contract_name == "session_orchestrator"
    assert result.dispatch_result == {"status": "complete", "dispatch_queue": []}

    assert server.request_line is not None
    request = json.loads(server.request_line.decode("utf-8").strip())
    assert request["node_alias"] == "session_orchestrator"
    assert request["payload"] == {"dry_run": True}
    assert request["timeout_ms"] == 1234
    with suppress(FileNotFoundError):
        socket_path.unlink()


def test_main_returns_zero_for_ok_response(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    socket_path = _socket_path("omnimarket-runtime-main-ok")
    payload_file = tmp_path / "payload.json"
    payload_file.write_text('{"dry_run": true}', encoding="utf-8")
    response = {
        "ok": True,
        "node_alias": "pr_lifecycle_orchestrator",
        "resolved_node_name": "node_pr_lifecycle_orchestrator",
        "contract_name": "pr_lifecycle_orchestrator",
        "dispatch_result": {"final_state": "COMPLETE"},
    }
    server = _OneShotSocketServer(socket_path, response)
    server.start()
    assert server.ready.wait(timeout=2.0)

    rc = main(
        [
            "--node-alias",
            "pr_lifecycle_orchestrator",
            "--payload-file",
            str(payload_file),
            "--socket-path",
            str(socket_path),
        ]
    )

    server.join(timeout=2.0)
    assert rc == 0
    captured = capsys.readouterr()
    assert '"ok": true' in captured.out.lower()
    assert '"final_state": "COMPLETE"' in captured.out
    with suppress(FileNotFoundError):
        socket_path.unlink()


def test_main_returns_one_for_runtime_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    socket_path = _socket_path("omnimarket-runtime-main-error")
    response = {
        "ok": False,
        "node_alias": "aislop_sweep",
        "error": {
            "code": "runtime_unavailable",
            "message": "runtime is draining",
            "retryable": True,
        },
    }
    server = _OneShotSocketServer(socket_path, response)
    server.start()
    assert server.ready.wait(timeout=2.0)

    rc = main(
        [
            "--node-alias",
            "aislop_sweep",
            "--payload",
            '{"repos":["omnimarket"],"dry_run":true}',
            "--socket-path",
            str(socket_path),
        ]
    )

    server.join(timeout=2.0)
    assert rc == 1
    captured = capsys.readouterr()
    assert '"code": "runtime_unavailable"' in captured.out
    with suppress(FileNotFoundError):
        socket_path.unlink()
