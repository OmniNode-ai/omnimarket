# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Installed picker proves one-token availability through real loopback HTTP.

The endpoint is a controlled protocol fixture, not an AI inference proof.
Evidence records request order, consent and selected models without credentials.
"""

import json
import os
import subprocess
import sys
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

pytestmark = pytest.mark.e2e


@contextmanager
def endpoint(statuses: dict[str, int]):
    calls = []

    class Endpoint(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def send(self, status, body):
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            self.send(200, {"data": [{"id": model} for model in statuses]})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(body)
            status = statuses[body["model"]]
            self.send(
                status,
                {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
                if status == 200
                else {
                    "error": {
                        "message": "deprecated model"
                        if status == 404
                        else "busy provider"
                    }
                },
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def invoke(tmp_path: Path, url: str, answer: str):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return subprocess.run(
        [
            str(Path(sys.executable).parent / "onex"),
            "setup",
            "--interactive",
            "--workspace",
            str(tmp_path / "workspace"),
            "--component",
            "delegation",
            "--provider",
            "ollama",
            "--url",
            url,
        ],
        cwd=tmp_path,
        env={
            "HOME": str(home),
            "PATH": str(Path(sys.executable).parent)
            + ":"
            + os.environ.get("PATH", "/usr/bin:/bin"),
            "ONEX_STATE_DIR": str(tmp_path / "state"),
        },
        input=answer,
        capture_output=True,
        text=True,
        timeout=35,
        check=False,
    )


def test_picker_excludes_404_orders_throttle_and_preserves_user_order(
    tmp_path: Path,
) -> None:
    with endpoint({"busy-model": 429, "gone-model": 404, "ready-model": 200}) as (
        url,
        calls,
    ):
        declined = invoke(tmp_path, url, "n\n")
        assert declined.returncode != 0
        assert calls == []
        result = invoke(tmp_path, url, "y\nbusy-model,ready-model\nn\n")
    assert len(calls) == 3, result.stdout + result.stderr
    assert all(body.get("max_completion_tokens") == 1 for body in calls)
    assert "deprecated model" in result.stderr
    assert "gone-model" not in result.stdout
    assert result.stdout.index("ready-model") < result.stdout.index("busy-model")
    assert "Models: ollama: busy-model, ready-model" in result.stdout
    assert result.returncode != 0  # User declined the apply, after choosing.
    assert not (tmp_path / "workspace").exists()
    (tmp_path / "model-probe-evidence.json").write_text(
        json.dumps(
            {
                "calls": calls,
                "excluded": ["gone-model"],
                "selected": ["busy-model", "ready-model"],
                "no_request_without_probe_consent": True,
                "applied": False,
            },
            indent=2,
        )
    )


def test_all_throttled_remain_selectable_with_busy_notice(tmp_path: Path) -> None:
    with endpoint({"first": 429, "second": 429}) as (url, calls):
        result = invoke(tmp_path, url, "y\nsecond,first\nn\n")
    assert len(calls) == 2, result.stdout + result.stderr
    assert "all listed models are busy" in result.stdout.lower()
    assert "Models: ollama: second, first" in result.stdout
    assert not (tmp_path / "workspace").exists()
