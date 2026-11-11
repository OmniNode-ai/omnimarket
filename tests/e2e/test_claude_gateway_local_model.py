# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Opt-in installed ONEX and Claude against a real native Messages backend.

Failure modes: non-native route, unreachable gateway, rejected native request,
missing answer, missing usage/projection, credential disclosure, orphan child.
The backend is pinned in an isolated canonical store, never the user's routing.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import signal
import socket
import sqlite3
import sys
from pathlib import Path

import httpx
import pytest

from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.transport import (
    post_chat_completion,
)

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]


async def _execute(
    argv: list[str],
    env: dict[str, str],
    cwd: Path,
    *,
    stdin: str = "",
    timeout: int = 120,
) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(stdin.encode()), timeout
        )
    finally:
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGKILL)
            await asyncio.wait_for(process.wait(), 10)
    return process.returncode or 0, stdout.decode(), stderr.decode()


async def test_installed_claude_uses_actual_native_local_model(tmp_path: Path) -> None:
    endpoint = os.environ.get("ONEX_NATIVE_MESSAGES_ENDPOINT")
    model = os.environ.get("ONEX_NATIVE_MESSAGES_MODEL")
    if not endpoint or not model:
        pytest.skip(
            "Set ONEX_NATIVE_MESSAGES_ENDPOINT and ONEX_NATIVE_MESSAGES_MODEL for the explicit local proof"
        )
    claude = shutil.which("claude")
    assert claude, "Installed Claude Code is required for this explicit proof"
    onex = str(Path(sys.executable).parent / "onex")
    home, workspace, state = (
        tmp_path / "home",
        tmp_path / "workspace",
        tmp_path / "state",
    )
    home.mkdir()
    workspace.mkdir()
    env = {
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "ONEX_STATE_DIR": str(state),
        "PATH": os.pathsep.join(
            (str(Path(onex).parent), str(Path(claude).parent), "/usr/bin", "/bin")
        ),
        "LANG": "en_US.UTF-8",
        "SHELL": "/bin/zsh",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "1024",
        "MAX_THINKING_TOKENS": "0",
    }
    code, stdout, stderr = await _execute(
        [onex, "local", "init", "--json"], env, workspace
    )
    assert code == 0, stderr
    store = LocalByokCredentialStore(db_path=Path(json.loads(stdout)["store"]))
    key = secrets.token_urlsafe(32)
    code, _, stderr = await _execute(
        [onex, "secret", "set", "gateway.proof"], env, workspace, stdin=key
    )
    assert code == 0, stderr
    overlay = {
        "backends": [
            {
                "backend_id": "native-proof",
                "provider": "local",
                "endpoint_url": endpoint,
                "model_name": model,
                "tier": "local",
                "timeout_ms": 120000,
                "max_tokens": 4096,
                "secret_ref": None,
                "explicit_pin_only": True,
            }
        ]
    }
    await store.set_secret("delegation.bifrost.overlay", json.dumps(overlay))
    with socket.socket() as port_probe:
        port_probe.bind(("127.0.0.1", 0))
        port = port_probe.getsockname()[1]
    server = await asyncio.create_subprocess_exec(
        onex,
        "claude",
        "gateway",
        "--backend-id",
        "native-proof",
        "--model",
        model,
        "--credential-ref",
        "gateway.proof",
        "--port",
        str(port),
        cwd=workspace,
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            async with asyncio.timeout(20):
                while True:
                    assert server.returncode is None, (
                        "Gateway process exited before readiness"
                    )
                    try:
                        response = await client.get(
                            url + "/onex/gateway/protocol",
                            headers={"authorization": "Bearer " + key},
                            timeout=1,
                        )
                        if response.status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    await asyncio.sleep(0.1)
            response = await asyncio.to_thread(
                post_chat_completion,
                endpoint_url=url + "/v1/messages",
                extra_headers={"authorization": "Bearer " + key},
                payload={
                    "model": model,
                    "messages": [{"role": "user", "content": "Say ready"}],
                    "max_tokens": 32,
                },
                timeout_seconds=120,
                runtime_profile="main",
            )
            assert response.status_code == 200, response.json_body
        code, stdout, stderr = await _execute(
            [
                onex,
                "claude",
                "enable",
                "--gateway-url",
                url,
                "--model",
                model,
                "--credential-ref",
                "gateway.proof",
                "--yes",
                "--json",
            ],
            env,
            workspace,
        )
        assert code == 0, stdout + stderr
        code, stdout, stderr = await _execute(
            [
                onex,
                "claude",
                "start",
                "--",
                "-p",
                "What is 6 times 7? Reply with only the number.",
                "--output-format",
                "json",
                "--tools",
                "",
                "--no-session-persistence",
            ],
            env,
            workspace,
        )
        assert key not in stdout + stderr
        assert code == 0, stdout[-1500:] + stderr[-1500:]
        answer = json.loads(stdout)
        assert not answer.get("is_error"), answer.get("result")
        assert answer["result"].strip() == "42"
        with sqlite3.connect(store.db_path.as_uri() + "?mode=ro", uri=True) as database:
            rows = database.execute(
                "SELECT correlation_id, model_name, tokens_input, tokens_output, cost_usd FROM delegation_events WHERE backend_id = 'native-proof'"
            ).fetchall()
        assert rows
        assert all(
            row[1] == model and row[2] > 0 and row[3] > 0 and row[4] is None
            for row in rows
        )
        manifest = {
            "upstream": "actual local native model",
            "model": model,
            "run_ids": [row[0] for row in rows],
            "usage": [
                {"input_tokens": row[2], "output_tokens": row[3], "cost_usd": row[4]}
                for row in rows
            ],
            "store": str(store.db_path),
            "state_root": str(state),
        }
        (state / "native-local-proof.json").write_text(json.dumps(manifest, indent=2))
        export = os.environ.get("ONEX_REAL_CLAUDE_EVIDENCE")
        if export:
            Path(export).mkdir(parents=True, exist_ok=True)
            (Path(export) / "native-local-proof.json").write_text(
                json.dumps(manifest, indent=2)
            )
    finally:
        if server.returncode is None:
            server.terminate()
            try:
                await asyncio.wait_for(server.wait(), 10)
            except TimeoutError:
                server.kill()
                await server.wait()
