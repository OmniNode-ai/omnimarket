# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex setup`` finishes on a clean machine: no ``ONEX_*`` variables, an empty home, a fresh empty workspace (OMN-26451).

The installed command runs under ``env -i`` against a stub model server on the loopback with the flags a
customer's first run uses. On a machine that declares no hosted bus the runtime bus forward node has no overlay;
setup used to count its undelivered events as a failure and exit 1 before it wrote anything.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

MODEL = "stub-model"


class _StubModelServer(BaseHTTPRequestHandler):
    """The two reads setup makes of a local OpenAI-compatible model server."""

    def _answer(self, document: dict[str, object]) -> None:
        body = json.dumps(document).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._answer({"object": "list", "data": [{"id": MODEL, "object": "model"}]})

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._answer(
            {
                "id": "stub",
                "object": "chat.completion",
                "model": MODEL,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
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

    def log_message(self, format: str, *args: object) -> None:
        del format, args


def test_setup_exits_zero_and_writes_its_configuration_on_a_clean_machine(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    venv_bin = Path(sys.executable).parent
    onex = venv_bin / "onex"
    assert onex.is_file(), f"the onex command is not installed beside {sys.executable}"
    server = HTTPServer(("127.0.0.1", 0), _StubModelServer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        completed = subprocess.run(
            [
                "env",
                "-i",
                f"HOME={home}",
                f"PATH={venv_bin}:/usr/bin:/bin",
                str(onex),
                "setup",
                "--workspace",
                str(workspace),
                "--path",
                "local",
                "--provider",
                "local",
                "--url",
                url,
                "--model",
                MODEL,
                "--component",
                "delegation",
                "--no-interactive",
                "--yes",
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    finally:
        server.shutdown()
        server.server_close()
    assert completed.returncode == 0, (
        completed.stdout[-1500:] + completed.stderr[-1500:]
    )
    assert "were not delivered" not in completed.stderr
    assert (home / ".onex" / "setup.json").is_file()
    assert any(workspace.iterdir()), "setup wrote nothing into the workspace"
