# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Serve one morning phase's deterministic tools to one agent session over MCP stdio.

The session is started with every built-in tool off and this server as its only tool
source, so the tools listed here are the whole of what the agent can do. Each call is
validated against the tool's typed input, run by the tool, and recorded in the run's
call log, one JSON line per call.

    python -m omnimarket.nodes.node_morning_ground_state_orchestrator.tools.server \
        --context <context.json> --calls <calls.jsonl>
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import TextIO, cast

from pydantic import JsonValue, ValidationError

from .base import MorningTool, MorningToolRefusalError
from .context import ModelMorningToolContext
from .phases import phase_tools

SERVER_NAME = "morning"
PROTOCOL_VERSION = "2025-06-18"
#: Above this a result would be cut by the session (MCP output limit); the agent pages instead.
MAX_RESULT_CHARS = 60_000


class HandlerMorningToolServer:
    def __init__(self, ctx: ModelMorningToolContext, calls: Path | None = None) -> None:
        self.ctx = ctx
        self.tools: dict[str, MorningTool] = {
            t.name: t for t in phase_tools(ctx.phase_label)
        }
        self.calls = calls

    def list_tools(self) -> list[dict[str, JsonValue]]:
        return [
            {
                "name": t.name,
                "description": t.description,
                "inputSchema": t.input_schema(),
            }
            for t in self.tools.values()
        ]

    def call(self, name: str, arguments: dict[str, JsonValue]) -> tuple[bool, str]:
        started = time.monotonic()
        tool = self.tools.get(name)
        ok = False
        if tool is None:
            text = f"no such tool for phase {self.ctx.phase_label}: {name}"
        else:
            try:
                params = tool.input_model.model_validate(arguments)
                text = json.dumps(tool.run(self.ctx, params), sort_keys=True)
                ok = True
                if len(text) > MAX_RESULT_CHARS:
                    ok = False
                    text = (
                        f"{name} result is {len(text)} characters, over the "
                        f"{MAX_RESULT_CHARS} a call may return: narrow the input or page it"
                    )
            except ValidationError as exc:
                text = f"invalid input for {name}: {exc}"
            except MorningToolRefusalError as exc:
                text = f"REFUSED: {exc}"
            except (
                Exception
            ) as exc:  # a tool fault is reported to the agent, not raised
                text = f"{name} failed: {type(exc).__name__}: {exc}"
        if self.calls is not None:
            record = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "phase": self.ctx.phase_label,
                "tool": name,
                "arguments": arguments,
                "ok": ok,
                "seconds": round(time.monotonic() - started, 3),
                "result_chars": len(text),
                **({} if ok else {"error": text[:600]}),
            }
            with self.calls.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, sort_keys=True) + "\n")
        return ok, text

    def handle(self, message: dict[str, JsonValue]) -> dict[str, JsonValue] | None:
        method = message.get("method")
        mid = message.get("id")
        if mid is None:
            return None  # a notification
        params = cast(dict[str, JsonValue], message.get("params") or {})
        if method == "initialize":
            result: dict[str, JsonValue] = {
                "protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": "1"},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": cast(JsonValue, self.list_tools())}
        elif method == "tools/call":
            ok, text = self.call(
                str(params.get("name", "")),
                cast(dict[str, JsonValue], params.get("arguments") or {}),
            )
            result = {"content": [{"type": "text", "text": text}], "isError": not ok}
        else:
            return {
                "jsonrpc": "2.0",
                "id": mid,
                "error": {"code": -32601, "message": f"method not found: {method}"},
            }
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def serve(self, stdin: TextIO, stdout: TextIO) -> None:
        for line in stdin:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            reply = self.handle(message)
            if reply is not None:
                stdout.write(json.dumps(reply) + "\n")
                stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="morning-phase-tools")
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--calls", type=Path, default=None)
    args = parser.parse_args(argv)
    ctx = ModelMorningToolContext.model_validate_json(
        args.context.read_text(encoding="utf-8")
    )
    HandlerMorningToolServer(ctx, args.calls).serve(sys.stdin, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
