# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Actual hook CLI, persisted bit-11 switch and gateway discovery; no model calls.

Failure modes: wrong gate bit, stale persisted mask, gateway outage allowed,
settings bypass allowed, duplicate inference, and prompt/credential disclosure.
"""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e


def test_hook_obeys_persisted_switch_without_duplicate_inference(
    tmp_path: Path,
) -> None:
    home, workspace = tmp_path / "home", tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    env = {
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "ONEX_STATE_DIR": str(tmp_path / "state"),
        "OMNIBASE_ENV_FILE": str(home / ".omnibase" / ".env"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    onex = str(Path(sys.executable).parent / "onex")
    commands: list[str] = []

    def run(*args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [onex, *args],
            input=stdin,
            env=env,
            cwd=workspace,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert "credential-SENTINEL" not in result.stdout + result.stderr
        assert "prompt-SENTINEL" not in result.stdout + result.stderr
        return result

    class Discovery(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            commands.append(self.path)
            if self.headers.get("authorization") != "Bearer credential-SENTINEL":
                self.send_response(401)
                self.end_headers()
                return
            body = (
                {
                    "protocol": "onex-claude-gateway.v1",
                    "streaming": True,
                    "tool_use": True,
                    "receipts": True,
                }
                if self.path == "/onex/gateway/protocol"
                else {"data": [{"id": "fixture-model"}]}
            )
            wire = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(wire)))
            self.end_headers()
            self.wfile.write(wire)

        def log_message(self, *_args: object) -> None:
            pass

    payload = json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "cwd": str(workspace),
            "tool_name": "Agent",
            "tool_input": {"prompt": "prompt-SENTINEL"},
        }
    )
    assert run("claude", "hook", stdin=payload).returncode == 0
    assert run("local", "init", "--json").returncode == 0
    assert (
        run("secret", "set", "gateway.hook", stdin="credential-SENTINEL").returncode
        == 0
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), Discovery)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        enabled = run(
            "claude",
            "enable",
            "--gateway-url",
            url,
            "--model",
            "fixture-model",
            "--credential-ref",
            "gateway.hook",
            "--yes",
            "--json",
        )
        assert enabled.returncode == 0, enabled.stdout + enabled.stderr
        allowed = run("claude", "hook", stdin=payload)
        assert allowed.returncode == 0
        assert not allowed.stdout
        override = workspace / ".claude" / "settings.local.json"
        override.parent.mkdir()
        override.write_text('{"env":{"ANTHROPIC_BASE_URL":"https://bypass.invalid"}}')
        denied = run("claude", "hook", stdin=payload)
        assert denied.returncode == 2
        assert not denied.stdout
        override.write_text("{}")
        assert run("claude", "hook", stdin=payload).returncode == 0
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
    assert run("claude", "hook", stdin=payload).returncode == 2
    assert run("hooks", "disable", "POST_SKILL_DELEGATION_ENFORCER").returncode == 0
    assert run("claude", "hook", stdin=payload).returncode == 0
    assert run("hooks", "enable", "POST_SKILL_DELEGATION_ENFORCER").returncode == 0
    assert run("claude", "hook", stdin=payload).returncode == 2
    assert commands
    assert set(commands) == {"/onex/gateway/protocol", "/v1/models"}
    for path in home.rglob("*"):
        if path.is_file():
            assert b"prompt-SENTINEL" not in path.read_bytes()
