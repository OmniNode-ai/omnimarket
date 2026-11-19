# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Installed command reaches a real HTTP catalog without inference or provisioning.

The fixture server proves discovery transport and CLI wiring, not model inference.
Its recorded requests and CLI JSON form a repeatable discovery artifact.
"""

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e


def test_installed_models_discover_reads_server_catalog(tmp_path: Path) -> None:
    requests: list[str] = []
    model_ids = ["user-running:custom", "organization/new-model"]

    class Catalog(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            body = json.dumps({"data": [{"id": name} for name in model_ids]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Catalog)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    home = tmp_path / "home"
    home.mkdir()
    try:
        result = subprocess.run(
            [
                str(Path(sys.executable).parent / "onex"),
                "models",
                "discover",
                "--url",
                f"http://127.0.0.1:{server.server_port}/v1",
                "--json",
            ],
            env={
                "HOME": str(home),
                "PATH": "/usr/bin:/bin",
                "ONEX_STATE_DIR": str(home / "state"),
            },
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert result.returncode == 0, result.stdout + result.stderr
    output = json.loads(result.stdout)
    assert output["status"] == "ready"
    assert output["models"] == model_ids
    assert requests == ["/v1/models"]
    assert not list(home.iterdir())
    (tmp_path / "discovery-evidence.json").write_text(
        json.dumps(
            {
                "requests": requests,
                "result": output,
                "exit_code": result.returncode,
                "scope": "HTTP catalog discovery; no inference proof",
            },
            indent=2,
        )
    )
