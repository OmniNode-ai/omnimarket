# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Installed CLI and Claude Code, real HTTP and evidence; upstream is a synthetic fixture."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import sqlite3
import sys
from pathlib import Path

import httpx
import pytest
import uvicorn

from omnimarket.handlers import claude_gateway as composition
from omnimarket.handlers.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
)
from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore
from omnimarket.models.delegation.model_claude_gateway import ModelClaudeGatewayConfig

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]


async def execute(
    argv: list[str],
    env: dict[str, str],
    cwd: Path,
    *,
    stdin: str = "",
    timeout: float = 60,
) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(stdin.encode()), timeout
        )
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    return process.returncode or 0, stdout.decode(), stderr.decode()


async def test_gateway_cli_lifecycle_command_exists() -> None:
    onex = Path(sys.executable).parent / "onex"
    process = await asyncio.create_subprocess_exec(
        str(onex),
        "claude",
        "gateway",
        "--help",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode()
    assert b"--backend-id" in stdout


@pytest.mark.parametrize(
    "flag",
    ["--bare", "--restricted", "--cloud", "--desktop", "--environment", "--teleport"],
)
async def test_cli_refuses_modes_that_ignore_gateway_settings(
    tmp_path: Path, flag: str
) -> None:
    onex = Path(sys.executable).parent / "onex"
    env = {"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    code, _, stderr = await execute(
        [str(onex), "claude", "start", "--", flag], env, tmp_path
    )
    assert code != 0
    assert "Routing override" in stderr


@pytest.mark.parametrize("tool", ["Read", "Agent"])
async def test_real_claude_tool_cycle_uses_gateway_and_preserves_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    claude = shutil.which("claude")
    assert claude, "This explicit proof requires an installed Claude Code binary"
    onex = str(Path(sys.executable).parent / "onex")
    home, workspace, state = (
        tmp_path / "home",
        tmp_path / "workspace",
        tmp_path / "state",
    )
    home.mkdir()
    workspace.mkdir()
    fixture = workspace / "fixture.txt"
    fixture.write_text("fixture-answer\n")
    env = {
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "ONEX_STATE_DIR": str(state),
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "PATH": os.pathsep.join(
            (str(Path(onex).parent), str(Path(claude).parent), "/usr/bin", "/bin")
        ),
        "LANG": "en_US.UTF-8",
        "SHELL": "/bin/zsh",
    }
    code, stdout, stderr = await execute(
        [onex, "local", "init", "--json"], env, workspace
    )
    assert code == 0, stderr
    store = LocalByokCredentialStore(db_path=Path(json.loads(stdout)["store"]))
    for reference, value in (
        ("gateway.fixture", "gateway-SENTINEL"),
        ("upstream.fixture", "upstream-SENTINEL"),
    ):
        code, _, stderr = await execute(
            [onex, "secret", "set", reference], env, workspace, stdin=value
        )
        assert code == 0, stderr
    backend = ModelResolvedDelegationBackend(
        backend_id="fixture-native",
        model_id="claude-fixture",
        endpoint_ref="https://upstream.example/v1/messages",
        max_tokens=8192,
        timeout_ms=30000,
        secret_ref="upstream.fixture",
    )
    monkeypatch.setattr(
        composition, "resolve_delegation_backend", lambda *_a, **_kw: backend
    )
    captured: list[dict[str, object]] = []
    issued = False

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal issued
        body = json.loads(request.content)
        messages = body.get("messages", [])
        all_text = json.dumps(messages)
        tool_result = any(
            isinstance(message.get("content"), list)
            and any(
                isinstance(block, dict) and block.get("type") == "tool_result"
                for block in message["content"]
            )
            for message in messages
        )
        available = {entry.get("name") for entry in body.get("tools", [])}
        main = "ONEX_GATEWAY_E2E_MAIN" in all_text
        child = "ONEX_GATEWAY_E2E_CHILD" in all_text
        captured.append(
            {
                "model": body.get("model"),
                "tool_result": tool_result,
                "main": main,
                "child": child,
            }
        )
        assert request.headers["x-api-key"] == "upstream-SENTINEL"
        should_use_tool = main and tool in available and not issued
        if should_use_tool:
            issued = True
            arguments = (
                {"file_path": str(fixture)}
                if tool == "Read"
                else {
                    "description": "Fixture agent",
                    "prompt": "ONEX_GATEWAY_E2E_CHILD: say fixture-answer",
                    "subagent_type": "general-purpose",
                }
            )
            block: dict[str, object] = {
                "type": "tool_use",
                "id": "tool_fixture",
                "name": tool,
                "input": {},
            }
            delta: dict[str, object] = {
                "type": "input_json_delta",
                "partial_json": json.dumps(arguments),
            }
        else:
            block = {"type": "text", "text": ""}
            delta = {"type": "text_delta", "text": "fixture-answer"}
        events = [
            {
                "type": "message_start",
                "message": {
                    "id": "msg_fixture_" + str(len(captured)),
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-fixture",
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 3, "output_tokens": 0},
                },
            },
            {"type": "content_block_start", "index": 0, "content_block": block},
            {"type": "content_block_delta", "index": 0, "delta": delta},
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {
                    "stop_reason": "tool_use" if should_use_tool else "end_turn",
                    "stop_sequence": None,
                },
                "usage": {"output_tokens": 5},
            },
            {"type": "message_stop"},
        ]
        wire = b"".join(
            (f"event: {event['type']}\ndata: {json.dumps(event)}\n\n").encode()
            for event in events
        )
        return httpx.Response(
            200, content=wire, headers={"content-type": "text/event-stream"}
        )

    app = composition.build_local_gateway_application(
        ModelClaudeGatewayConfig(
            backend_id="fixture-native",
            model="claude-fixture",
            credential_ref="gateway.fixture",
        ),
        state_root=state,
        store=store,
        transport=httpx.MockTransport(respond),
    )
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="warning"))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(0.01)
        code, stdout, stderr = await execute(
            [
                onex,
                "claude",
                "enable",
                "--gateway-url",
                f"http://127.0.0.1:{port}",
                "--model",
                "claude-fixture",
                "--credential-ref",
                "gateway.fixture",
                "--yes",
                "--json",
            ],
            env,
            workspace,
        )
        assert code == 0, stdout + stderr
        assert json.loads(stdout)["enabled"]
        if tool == "Agent":
            code, stdout, stderr = await execute(
                [onex, "claude", "hooks", "install", "--yes", "--json"], env, workspace
            )
            assert code == 0, stdout + stderr
            assert json.loads(stdout)["registered"]
        code, stdout, stderr = await execute(
            [
                onex,
                "claude",
                "start",
                "--",
                "-p",
                "ONEX_GATEWAY_E2E_MAIN: use the requested tool, then say fixture-answer",
                "--output-format",
                "json",
                "--tools",
                tool,
                "--allowedTools",
                tool,
                "--no-session-persistence",
            ],
            env,
            workspace,
        )
        assert code == 0, stdout[-3000:] + stderr[-3000:]
        assert json.loads(stdout)["result"] == "fixture-answer"
        assert issued
        assert any(row["tool_result"] for row in captured)
        assert len(captured) >= (3 if tool == "Agent" else 2)
        if tool == "Agent":
            assert any(row["child"] for row in captured)
        with sqlite3.connect(store.db_path.as_uri() + "?mode=ro", uri=True) as database:
            rows = database.execute(
                "SELECT correlation_id, model_name, tokens_input, tokens_output, cost_usd FROM delegation_events"
            ).fetchall()
        assert len(rows) == len(captured)
        assert all(row[1:] == ("claude-fixture", 3, 5, None) for row in rows)
        version_code, version, _ = await execute([claude, "--version"], env, workspace)
        assert version_code == 0
        manifest = {
            "upstream": "synthetic fixture",
            "claude_version": version.strip(),
            "tool": tool,
            "request_count": len(captured),
            "request_metadata": captured,
            "run_ids": [row[0] for row in rows],
            "state_root": str(state),
            "store": str(store.db_path),
        }
        (state / "real-client-proof.json").write_text(json.dumps(manifest, indent=2))
        export = os.environ.get("ONEX_REAL_CLAUDE_EVIDENCE")
        if export:
            Path(export).mkdir(parents=True, exist_ok=True)
            (Path(export) / f"real-client-{tool}.json").write_text(
                json.dumps(manifest, indent=2)
            )
        override = workspace / ".claude" / "settings.local.json"
        override.parent.mkdir()
        override.write_text('{"env":{"ANTHROPIC_BASE_URL":"https://bypass.invalid"}}')
        code, stdout, _ = await execute(
            [onex, "claude", "status", "--json"], env, workspace
        )
        assert code != 0
        assert json.loads(stdout)["status"] == "action_required"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        listener.close()
    code, _, stderr = await execute(
        [onex, "claude", "start", "--", "-p", "should not launch"], env, workspace
    )
    assert code != 0
    assert "unavailable" in stderr.lower() or "status" in stderr.lower()
