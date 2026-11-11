# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Registry-backed hosted picker journey through real HTTP; no real AI claims."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import click
import pytest
from click.testing import CliRunner
from pydantic import SecretStr

from omnimarket.handlers.handler_endpoint_catalog import HandlerEndpointCatalog
from omnimarket.handlers.handler_endpoint_probe import HandlerModelProbe
from omnimarket.handlers.routing import byok_provider_backends as registry
from omnimarket.nodes.node_onex_setup_effect.cli_choices import ask_choices

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("native", [False, True])
def test_hosted_picker_uses_declared_catalog_protocol_and_one_hidden_key(
    tmp_path, monkeypatch, native
):
    secret = "fixture-private-key-must-not-appear"
    calls = []

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def answer(self, status, body):
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            assert self.path == "/account/catalog"
            assert self.headers.get("x-api-key" if native else "Authorization") == (
                secret if native else "Bearer " + secret
            )
            calls.append(("GET", None))
            self.answer(
                200,
                {
                    "models": [
                        {"name": "models/" + x} for x in ("busy", "gone", "ready")
                    ]
                },
            )

        def do_POST(self):
            assert self.path == "/account/inference"
            assert self.headers.get("x-api-key" if native else "Authorization") == (
                secret if native else "Bearer " + secret
            )
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert body["max_tokens" if native else "max_completion_tokens"] == 1
            calls.append(("POST", body["model"]))
            status = {"busy": 429, "gone": 404, "ready": 200}[body["model"]]
            self.answer(
                status,
                {"content": [{"type": "text", "text": "yes"}]}
                if native and status == 200
                else {"choices": [{"message": {"content": "yes"}}]}
                if status == 200
                else {"error": {"message": "refused " + secret}},
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    backend = next(iter(registry.load_byok_provider_catalog().values())).model_copy(
        update={
            "provider": "fixture-hosted",
            "models_url": f"http://127.0.0.1:{server.server_port}/account/catalog",
            "endpoint_url": f"http://127.0.0.1:{server.server_port}/account/inference",
            "models_auth": "x_api_key" if native else "bearer",
            "models_headers": {},
            "inference_protocol": "anthropic_messages" if native else "openai_chat",
            "token_limit_field": "max_tokens" if native else "max_completion_tokens",
        }
    )
    monkeypatch.setattr(
        registry, "load_byok_provider_catalog", lambda: {backend.provider: backend}
    )
    credentials = {}
    reads = []
    key = SecretStr(secret)

    def read_key(provider, interactive):
        reads.append((provider, interactive))
        return key

    @click.command()
    def choose():
        chosen = ask_choices(
            backend.provider,
            None,
            None,
            discover=HandlerEndpointCatalog().discover,
            probe=HandlerModelProbe().probe,
            read_key=read_key,
            credentials=credentials,
        )
        click.echo(json.dumps(chosen))

    try:
        declined = CliRunner().invoke(choose, input="n\n")
        assert declined.exit_code != 0
        assert calls == [("GET", None)]
        calls.clear()
        reads.clear()
        result = CliRunner().invoke(choose, input="y\nbusy,ready\n")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    assert result.exit_code == 0, result.output
    assert calls == [
        ("GET", None),
        ("POST", "busy"),
        ("POST", "gone"),
        ("POST", "ready"),
    ]
    assert reads == [(backend.provider, True)]
    assert credentials[backend.provider] is key
    assert json.loads(result.output.splitlines()[-1]) == [
        backend.provider,
        "busy,ready",
        None,
    ]
    assert secret not in result.output
    assert "gone" not in result.stdout
    (tmp_path / "hosted-picker-evidence.json").write_text(
        json.dumps(
            {
                "protocol": backend.inference_protocol,
                "requests": calls,
                "hidden_key_reads": len(reads),
                "selected": ["busy", "ready"],
                "proof_kind": "controlled-provider-protocol",
            }
        )
    )
