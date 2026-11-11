# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Actual CLI reuses independent saved keys; one stdin cannot supply two new keys."""

import json
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

pytestmark = pytest.mark.e2e


def test_attribution_catalog_cache_isolates_credentials(tmp_path: Path) -> None:
    from omnimarket.nodes.node_llm_delegation_call_effect.handlers import transport

    reads: list[str] = []

    class Catalog(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            return

        def do_GET(self) -> None:
            identity = {"Bearer first": "one", "Bearer second": "two"}.get(
                self.headers.get("Authorization", ""), "public"
            )
            reads.append(identity)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"data": [{"id": identity}]}).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Catalog)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    endpoint = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
    try:
        transport.served_models_cache.clear()
        for key, model in (("first", "one"), ("second", "two"), ("first", "one")):
            assert transport.get_served_model_ids(
                endpoint, extra_headers={"Authorization": "Bearer " + key}
            ) == frozenset({model})
        assert transport.get_served_model_ids(endpoint) == frozenset({"public"})
        assert reads == ["one", "two", "public"]
        assert "Bearer" not in repr(transport.served_models_cache)
        (tmp_path / "attribution-cache-proof.json").write_text(
            json.dumps(
                {
                    "catalog_reads": reads,
                    "cache_entries": len(transport.served_models_cache),
                }
            )
        )
    finally:
        transport.served_models_cache.clear()
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


@pytest.mark.parametrize("saved_keys", [False, True])
def test_unattended_setup_counts_only_keys_missing_from_saved_connections(
    tmp_path: Path, saved_keys: bool
) -> None:
    calls: list[dict[str, object]] = []
    expected: dict[int, tuple[str, str]] = {}

    class Endpoint(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            return

        def reply(self, body: object) -> None:
            secret, model = expected[self.server.server_port]
            valid = self.headers.get("Authorization") == "Bearer " + secret
            calls.append(
                {
                    "valid_key": valid,
                    "has_key": self.headers.get("Authorization") is not None,
                    "model": model,
                    "path": self.path,
                }
            )
            self.send_response(200 if valid else 401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def do_GET(self) -> None:
            self.reply({"data": [{"id": expected[self.server.server_port][1]}]})

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            model = expected[self.server.server_port][1]
            assert body["model"] == model
            self.reply(
                {
                    "id": "fixture",
                    "object": "chat.completion",
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "hello"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 5,
                        "completion_tokens": 1,
                        "total_tokens": 6,
                    },
                }
            )

    servers = [ThreadingHTTPServer(("127.0.0.1", 0), Endpoint) for _ in range(2)]
    workers: list[Thread] = []
    providers: list[dict[str, object]] = []
    home = tmp_path / "home"
    home.mkdir()
    cli = str(Path(sys.executable).parent / "onex")
    env = {
        "HOME": str(home),
        "PATH": str(Path(cli).parent) + ":/usr/bin:/bin",
        "ONEX_STATE_DIR": str(tmp_path / "state"),
    }

    def run(*args: str, secret: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [cli, *args],
            cwd=home,
            env=env,
            input=secret,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    try:
        for index, server in enumerate(servers):
            name, model, secret = (
                f"server-{index}",
                f"custom-model-{index}",
                f"private-sentinel-{index}",
            )
            expected[server.server_port] = secret, model
            worker = Thread(target=server.serve_forever, daemon=True)
            worker.start()
            workers.append(worker)
            url = f"http://127.0.0.1:{server.server_port}"
            providers.append(
                {
                    "provider": "openai_compatible",
                    "connection_id": name,
                    "models": [model],
                    "url": url,
                    "api_key_required": True,
                }
            )
            if saved_keys:
                connected = run(
                    "models",
                    "connect",
                    name,
                    "--url",
                    url,
                    "--model",
                    model,
                    "--api-key",
                    "--no-test",
                    "--json",
                    secret=secret,
                )
                assert connected.returncode == 0, connected.stdout + connected.stderr
        plan = tmp_path / "plan.json"
        plan.write_text(
            json.dumps(
                {
                    "workspace": str(tmp_path / "workspace"),
                    "components": ["delegation"],
                    "providers": providers,
                }
            )
        )
        result = run(
            "setup", "--plan", str(plan), "--yes", "--no-interactive", "--json"
        )
        output = json.loads(result.stdout)
        if saved_keys:
            assert result.returncode == 0, result.stdout + result.stderr
            assert all(item["status"] == "success" for item in output["observations"])
            assert {
                row["model"]
                for row in calls
                if row["path"].endswith("/chat/completions")
            } == {"custom-model-0", "custom-model-1"}
            assert all(
                row["valid_key"] or (row["path"] == "/health" and not row["has_key"])
                for row in calls
            ), [
                row
                for row in calls
                if not row["valid_key"] and row["path"] != "/health"
            ]
        else:
            assert result.returncode != 0
            assert output["status"] == "refused"
            assert "Multiple provider keys" in output["message"]
            assert not calls
            assert not (tmp_path / "workspace").exists()
        assert "private-sentinel" not in result.stdout + result.stderr
        (tmp_path / "saved-key-proof.json").write_text(
            json.dumps(
                {
                    "saved_keys": saved_keys,
                    "exit_code": result.returncode,
                    "result": output,
                    "requests": calls,
                },
                indent=2,
            )
        )
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for worker in workers:
            worker.join(timeout=5)
