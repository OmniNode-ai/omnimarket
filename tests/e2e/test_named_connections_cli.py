# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Installed named-connection commands preserve server credential boundaries.

RED control: an absent package entrypoint cannot configure a connection. Real
subprocesses and HTTP catalogs verify secrets never cross servers or reach JSON;
they do not substitute for model inference proof.
"""

import json
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest


@pytest.mark.parametrize(
    ("failing_first", "failure_status"), [(False, 503), (True, 503), (True, 401)]
)
def test_named_connection_commands_from_installed_cli(
    tmp_path: Path, failing_first: bool, failure_status: int
) -> None:
    models = (["unavailable-model"] if failing_first else []) + [
        "previously-unknown-model"
    ]
    seen: list[bool] = []
    requests: list[dict[str, object]] = []

    class Catalog(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            valid = (
                self.headers.get("Authorization")
                == "Bearer private-connection-sentinel"
            )
            seen.append(valid)
            self.send_response(200 if valid else 401)
            self.end_headers()
            self.wfile.write(
                json.dumps({"data": [{"id": model} for model in models]}).encode()
            )

        def do_POST(self):
            valid = (
                self.headers.get("Authorization")
                == "Bearer private-connection-sentinel"
            )
            body = json.loads(
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
            )
            requests.append(
                {"authenticated": valid, "model": body.get("model"), "path": self.path}
            )
            if body.get("model") == "unavailable-model":
                self.send_response(failure_status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    b'{"error":{"message":"Model unavailable","code":"service_unavailable"}}'
                )
                return
            self.send_response(200 if valid else 401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "id": "connection-fixture",
                        "object": "chat.completion",
                        "model": "previously-unknown-model",
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": "### ANSWER\nNamed connections bind each endpoint to its own credential and selected models. A key is resolved only for its configured server. Separate identities prevent one endpoint from overwriting another server's configuration. The default selection chooses the exact named route while retaining unrelated registrations.",
                                },
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 100,
                            "completion_tokens": 60,
                            "total_tokens": 160,
                        },
                    }
                ).encode()
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Catalog)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "ONEX_STATE_DIR": str(tmp_path / "state"),
    }
    command = [str(Path(sys.executable).parent / "onex"), "models"]

    def run(*args: str, key: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*command, *args],
            env=env,
            cwd=tmp_path,
            input=key,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )

    try:
        configured = run(
            "connect",
            "my-server",
            "--url",
            f"http://127.0.0.1:{server.server_port}",
            *[argument for model in models for argument in ("--model", model)],
            "--api-key",
            "--no-test",
            "--json",
            key="private-connection-sentinel",
        )
        assert configured.returncode == 0, configured.stdout + configured.stderr
        assert (
            "private-connection-sentinel" not in configured.stdout + configured.stderr
        )
        parsed = json.loads(configured.stdout)
        assert parsed["connections"][0]["connection_id"] == "my-server"
        assert parsed["tests"] == []
        before = {
            str(p): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in home.rglob("*")
            if p.is_file()
        }
        listed = run("connections", "--json")
        assert listed.returncode == 0, listed.stderr
        assert json.loads(listed.stdout)["connections"] == parsed["connections"]
        assert before == {
            str(p): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in home.rglob("*")
            if p.is_file()
        }
        refreshed = run("refresh-connection", "my-server", "--json")
        assert refreshed.returncode == 0, refreshed.stderr
        assert json.loads(refreshed.stdout)["catalog"] == models
        assert seen == [True, True]
        selected = run("select-connection", "my-server", "--json")
        assert selected.returncode == 0, selected.stdout + selected.stderr
        delegated = subprocess.run(
            [
                command[0],
                "delegate",
                "Explain how named endpoint connections isolate credentials",
                "--task-type",
                "document",
                "--json",
            ],
            env=env,
            cwd=tmp_path,
            input="",
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        if failing_first and failure_status == 401:
            assert delegated.returncode != 0
            assert requests
            assert all(row["authenticated"] for row in requests)
            assert all(row["model"] == "unavailable-model" for row in requests)
            assert (
                run("remove-connection", "my-server", "--yes", "--json").returncode == 0
            )
            (tmp_path / "named-auth-refusal-evidence.json").write_text(
                json.dumps(
                    {
                        "status": "refused",
                        "requests": requests,
                        "next_model_called": False,
                    },
                    indent=2,
                )
            )
            return
        assert delegated.returncode == 0, delegated.stdout + delegated.stderr
        assert requests
        assert all(row["authenticated"] for row in requests)
        assert list(dict.fromkeys(row["model"] for row in requests)) == models
        artifacts = list((tmp_path / "state" / "runs").glob("*/receipt.json"))
        assert artifacts
        receipt = json.loads(artifacts[-1].read_text())
        assert receipt["backend_id"] in parsed["connections"][0].get(
            "backend_ids", []
        ) or receipt["backend_id"].startswith("connection-my-server-")
        assert receipt["model"] == "previously-unknown-model"
        assert (
            receipt["endpoint"]
            == f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
        )
        if failing_first:
            from omnimarket.nodes.node_model_setup_effect.models.model_endpoint_connection import (
                ModelEndpointConnection,
            )

            connection = ModelEndpointConnection.model_validate(
                parsed["connections"][0]
            )
            before_pin = len(requests)
            pinned = subprocess.run(
                [
                    command[0],
                    "delegate",
                    "Explain named connection credentials",
                    "--task-type",
                    "document",
                    "--backend-id",
                    connection.backend_id("unavailable-model"),
                    "--json",
                ],
                env=env,
                cwd=tmp_path,
                input="",
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
            assert pinned.returncode != 0
            assert requests[before_pin:]
            assert all(
                row["model"] == "unavailable-model" for row in requests[before_pin:]
            )
        removed = run("remove-connection", "my-server", "--yes", "--json")
        assert removed.returncode == 0, removed.stdout + removed.stderr
        assert json.loads(removed.stdout)["connections"] == []
        evidence = {
            "configured": parsed,
            "listed": json.loads(listed.stdout),
            "refreshed": json.loads(refreshed.stdout),
            "removed": json.loads(removed.stdout),
            "auth_verdicts": seen,
            "default_request": requests,
            "default_receipt": receipt,
        }
        text = json.dumps(evidence, indent=2)
        assert "private-connection-sentinel" not in text
        assert "private-connection-sentinel" in text + "private-connection-sentinel", (
            "Leak detector positive control"
        )
        (tmp_path / "named-cli-evidence.json").write_text(text)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
