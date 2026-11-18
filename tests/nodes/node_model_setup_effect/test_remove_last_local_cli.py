# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Replay the incident through installed onex commands with an isolated HOME."""

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.integration


class _LocalModel(BaseHTTPRequestHandler):
    def _send(self, payload: dict[str, object]) -> None:
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._send({"object": "list", "data": [{"id": "incident-model"}]})

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self._send(
            {
                "id": "incident-response",
                "object": "chat.completion",
                "model": "incident-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "### ANSWER\nhello",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }
        )

    def log_message(self, *_args: object) -> None:
        pass


def test_remove_last_local_then_delegate_with_isolated_home(tmp_path: Path) -> None:
    """A removed model gets a routing refusal, never a null-backends crash."""
    isolated = tmp_path / "home"
    isolated.mkdir()
    onex = Path(sys.executable).with_name("onex")
    env = {
        "HOME": str(isolated),
        "PATH": f"{onex.parent}:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "ONEX_STATE_DIR": str(tmp_path / "state"),
    }
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LocalModel)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def run(label: str, *args: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [str(onex), *args],
            env=env,
            cwd=isolated,
            capture_output=True,
            text=True,
            timeout=100,
            check=False,
        )
        (tmp_path / f"{label}.log").write_text(result.stdout + result.stderr)
        return result

    try:
        added = run(
            "add",
            "models",
            "add",
            "local",
            "--url",
            f"http://127.0.0.1:{server.server_port}",
            "--model",
            "incident-model",
        )
        assert added.returncode == 0, added.stdout + added.stderr
        removed = run("remove", "models", "remove", "local")
        assert removed.returncode == 0, removed.stdout + removed.stderr
        overlay = isolated / ".omninode/delegation/bifrost_overrides.yaml"
        assert yaml.safe_load(overlay.read_text()).get("backends", []) == []
        delegated = run(
            "delegate",
            "delegate",
            "--bus",
            "inmemory",
            "--locus",
            "in-process",
            "--backend-id",
            "local-url",
            "--result-only",
            "hello",
        )
        output = delegated.stdout + delegated.stderr
        assert delegated.returncode != 0
        assert (
            "No delegation backend 'local-url' with a populated endpoint_url" in output
        ), output
        assert "schema validation failed" not in output
        receipt_path = Path(output.split("(receipt: ", 1)[1].strip().removesuffix(")"))
        assert receipt_path.is_relative_to(tmp_path / "state")
        receipt = json.loads(receipt_path.read_text())
        # The console shortens a long message in the middle, so the overlay's path is read from the receipt,
        # which keeps the full error: the refusal still names the file the user has to fix.
        assert str(overlay) in json.dumps(receipt), receipt
    finally:
        server.shutdown()
        server.server_close()
