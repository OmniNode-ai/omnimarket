# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Gateway setup refuses incompatible routes before writing Claude settings.

Failures: a chat endpoint advertises native compatibility; missing upstream key
passes readiness; restarting duplicates processes; stop destroys later user edits.
"""

from __future__ import annotations

import fcntl
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from omnimarket.handlers.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
)
from omnimarket.models.delegation.model_claude_gateway import ModelClaudeGatewayConfig
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_claude_gateway import (
    build_gateway_app,
)

pytestmark = pytest.mark.e2e


def test_gateway_stop_refuses_a_concurrent_lifecycle_writer(tmp_path: Path) -> None:
    from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_gateway import (
        HandlerSetupGateway,
    )

    lock = tmp_path / "gateway-process.lock"
    with lock.open("w") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            HandlerSetupGateway(onex_home=tmp_path).stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("native", "has_key", "expected"),
    [(False, True, 503), (True, False, 503), (True, True, 200)],
)
async def test_protocol_readiness_checks_actual_selected_route(
    native: bool, has_key: bool, expected: int
) -> None:
    calls: list[httpx.Request] = []
    backend = ModelResolvedDelegationBackend(
        backend_id="chosen",
        model_id="user-model",
        endpoint_ref="http://127.0.0.1:9999/v1/"
        + ("messages" if native else "chat/completions"),
        max_tokens=64,
        timeout_ms=1000,
        secret_ref="chosen.key",
    )

    async def secret(reference: str) -> str | None:
        return {
            "gateway.key": "gateway-value",
            "chosen.key": "upstream-value" if has_key else None,
        }.get(reference)

    async def record(_receipt: object) -> None:
        pytest.fail("Readiness generated an inference receipt")

    app = build_gateway_app(
        ModelClaudeGatewayConfig(
            backend_id="chosen", model="user-model", credential_ref="gateway.key"
        ),
        resolve_backend=lambda: backend,
        read_secret=secret,
        record=record,
        transport=httpx.MockTransport(
            lambda request: calls.append(request) or httpx.Response(500)
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://localhost"
    ) as client:
        for path in ("/onex/gateway/protocol", "/v1/models"):
            response = await client.get(
                path, headers={"authorization": "Bearer gateway-value"}
            )
            assert response.status_code == expected, response.text
    assert not calls, "Readiness made a billable request"


def test_setup_gateway_proves_route_reuses_process_and_preserves_user_edits(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    cli = str(Path(sys.executable).parent / "onex")
    environment = {
        "HOME": str(home),
        "PATH": str(Path(cli).parent) + ":/usr/bin:/bin",
        "ONEX_STATE_DIR": str(tmp_path / "state"),
    }
    calls: list[str] = []
    model = "native-user-model"

    class NativeServer(BaseHTTPRequestHandler):
        def log_message(self, *_: object) -> None:
            return

        def reply(self, body: object) -> None:
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("x-onex-run-id", "11111111-1111-1111-1111-111111111111")
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            self.reply({"object": "list", "data": [{"id": model}]})

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            calls.append(self.path)
            assert body["model"] == model
            self.reply(
                {
                    "id": "msg_setup_proof",
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [{"type": "text", "text": "hello"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 5, "output_tokens": 1},
                }
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), NativeServer)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()

    def execute(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            env=environment,
            cwd=home,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )

    proof: dict[str, object] = {"model_evidence": "native HTTP fixture"}
    try:
        assert execute(cli, "local", "init", "--json").returncode == 0
        connected = execute(
            sys.executable,
            "-c",
            "import sys; from omnimarket.handlers.handler_endpoint_connections import HandlerModelConnections, ModelEndpointConnectionRequest; r=HandlerModelConnections().handle(ModelEndpointConnectionRequest(operation='connect', connection_id='native-fixture',url=sys.argv[1],models=(sys.argv[2],),native_messages=True,prove=False)); print(r.model_dump_json()); raise SystemExit(0 if r.status=='ready' else 1)",
            f"http://127.0.0.1:{server.server_port}",
            model,
        )
        assert connected.returncode == 0, connected.stdout + connected.stderr
        plan = tmp_path / "plan.json"
        plan.write_text(
            json.dumps(
                {
                    "workspace": str(tmp_path / "workspace"),
                    "components": ["claude-gateway"],
                    "gateway": {
                        "connection_id": "native-fixture",
                        "model": model,
                        "port": 0,
                    },
                }
            )
        )
        first = execute(cli, "setup", "--plan", str(plan), "--yes", "--json")
        proof["first"] = {
            "exit_code": first.returncode,
            "stdout": first.stdout,
            "stderr": first.stderr,
        }
        assert first.returncode == 0, first.stdout + first.stderr
        assert calls == ["/v1/messages"], (
            "Setup did not prove exactly one native request"
        )
        state = home / ".onex" / "gateway-process.json"
        settings = home / ".claude" / "settings.json"
        snapshot = (state.read_bytes(), settings.read_bytes())
        metadata = json.loads(state.read_text())
        run_id = metadata["proof_run_id"]
        assert str(UUID(run_id)) == run_id
        assert run_id != "11111111-1111-1111-1111-111111111111"
        observations = json.loads(first.stdout)["observations"]
        gateway = next(
            item for item in observations if item["step"] == "claude-gateway"
        )
        assert gateway["evidence"]["proof_run_id"] == run_id
        assert gateway["evidence"]["correlation_id"] == run_id
        dashboard = execute(cli, "dashboard", "start", "--json")
        assert dashboard.returncode == 0, dashboard.stdout + dashboard.stderr
        dashboard_url = json.loads(dashboard.stdout)["evidence"]["url"]
        secret = execute(
            sys.executable,
            "-c",
            "import asyncio; from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore; print(asyncio.run(LocalByokCredentialStore().get_secret('setup.dashboard.session')) or '')",
        )
        assert secret.returncode == 0
        assert secret.stdout.strip()
        with httpx.Client(trust_env=False, timeout=15) as client:
            served = client.get(
                dashboard_url
                + "/projection/onex.snapshot.projection.delegation.decisions.v1",
                params={"correlation_id": run_id},
                headers={"authorization": "Bearer " + secret.stdout.strip()},
            )
        assert served.status_code == 200, served.text
        rows = served.json()["rows"]
        assert len(rows) == 1
        row = rows[0]
        assert row["correlation_id"] == run_id
        assert row["model_name"] == model
        assert row["backend_id"] == metadata["backend_id"]
        assert row["tokens_input"] == 5
        assert row["tokens_output"] == 1
        assert row["tenant_id"] == metadata["tenant_id"]
        proof["served_projection"] = served.json()
        receipt = execute(
            sys.executable,
            "-c",
            "import os,sys; from pathlib import Path; from omnibase_infra.cli.receipt_mode import create_receipt_artifact_store; from omnibase_core.models.artifacts.model_artifact_ref import ModelArtifactRef; print(create_receipt_artifact_store(Path(os.environ['ONEX_STATE_DIR'])).read_blob(ModelArtifactRef(ref=sys.argv[1])).decode())",
            row["authority_source"],
        )
        assert receipt.returncode == 0, receipt.stderr
        canonical = json.loads(receipt.stdout)
        assert canonical["run_id"] == run_id
        assert canonical["model"] == model
        assert canonical["backend_id"] == metadata["backend_id"]
        assert canonical["status"] == "completed"
        proof["canonical_receipt"] = canonical
        second = execute(cli, "setup", "--plan", str(plan), "--yes", "--json")
        proof["second"] = {
            "exit_code": second.returncode,
            "stdout": second.stdout,
            "stderr": second.stderr,
        }
        assert second.returncode == 0, second.stdout + second.stderr
        assert snapshot == (state.read_bytes(), settings.read_bytes())
        assert calls == ["/v1/messages"], "No-op setup repeated inference"
        changed = json.loads(settings.read_text())
        changed["env"]["ANTHROPIC_MODEL"] = "later-user-choice"
        changed["userField"] = "preserved"
        settings.write_text(json.dumps(changed))
        stopped = execute(
            sys.executable,
            "-c",
            "from pathlib import Path; from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_gateway import stop_gateway; stop_gateway(Path.home()/'.onex')",
        )
        assert stopped.returncode == 0, stopped.stderr
        remaining = json.loads(settings.read_text())
        assert remaining["env"]["ANTHROPIC_MODEL"] == "later-user-choice"
        assert remaining["userField"] == "preserved"
        assert "ANTHROPIC_BASE_URL" not in remaining["env"]
        proof["outcome"] = "passed"
    finally:
        execute(cli, "dashboard", "stop", "--json")
        execute(
            sys.executable,
            "-c",
            "from pathlib import Path; from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_gateway import stop_gateway; stop_gateway(Path.home()/'.onex')",
        )
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
        (tmp_path / "gateway-setup.json").write_text(json.dumps(proof, indent=2))
