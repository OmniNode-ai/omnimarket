# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Convert one headless agent run's log into an OpenTelemetry GenAI trace (OMN-24941).

Pure: no clock, no file, no network. The log is the parsed stream-json (or session) records of one
run. Tool calls, their results and the final assistant message become OTLP/JSON spans named after the
GenAI semantic conventions, and the tools the run used become an MCP-style tools list.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any

from omnimarket.models.model_agent_trace_otel import (
    ModelAgentTraceConvertRequest,
    ModelAgentTraceConvertResult,
)

_BASE_NS = 1_700_000_000_000_000_000
_STEP_NS = 1_000_000
_STATUS_ERROR = 2
_SPAN_KIND_INTERNAL = 1
_SCOPE_NAME = "omnimarket.node_agent_trace_otel_compute"

_READ_ONLY_TOOLS = frozenset(
    {"Read", "Grep", "Glob", "ToolSearch", "WebFetch", "WebSearch", "TodoRead"}
)
_READ_ONLY_PROGRAMS = frozenset(
    {
        "ls",
        "cat",
        "grep",
        "rg",
        "head",
        "tail",
        "wc",
        "date",
        "find",
        "echo",
        "pwd",
        "uname",
        "uptime",
        "df",
        "free",
        "nproc",
        "sort",
        "uniq",
        "cut",
        "tr",
        "diff",
        "stat",
        "ps",
    }
)
_READ_ONLY_GIT = frozenset(
    {
        "log",
        "show",
        "diff",
        "status",
        "rev-parse",
        "ls-files",
        "grep",
        "symbolic-ref",
        "branch",
    }
)
_WRITE_GIT_BRANCH_FLAGS = frozenset({"-D", "-d", "-m", "-M", "-c", "-C"})
_WRITE_PROGRAMS = frozenset({"tee", "rm", "mv", "cp"})
_SEGMENT_SPLIT = re.compile(r"\|\||&&|\||;|\n")


def _sha_hex(seed: str, length: int) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()[:length]


def _timestamp_ns(record: dict[str, Any]) -> int | None:
    raw = record.get("timestamp")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int(parsed.timestamp() * 1_000_000_000)


def _attr(key: str, value: str | bool | int) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    return {"key": key, "value": {"stringValue": value}}


def _text_parts(role: str, text: str) -> str:
    return json.dumps(
        [{"role": role, "parts": [{"type": "text", "content": text}]}],
        ensure_ascii=False,
    )


def _clip(text: str, limit: int) -> str:
    return text[:limit]


def _block_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                pieces.append(str(block.get("text", "")))
            elif isinstance(block, str):
                pieces.append(block)
        return "\n".join(pieces)
    return ""


def _is_read_only_bash(command: str) -> bool:
    if ">" in command:
        return False
    segments = [s.strip() for s in _SEGMENT_SPLIT.split(command) if s.strip()]
    if not segments:
        return False
    for segment in segments:
        words = segment.split()
        program = words[0]
        if program in _WRITE_PROGRAMS:
            return False
        if program == "sed":
            if "-n" not in words:
                return False
        elif program == "git":
            if len(words) < 2 or words[1] not in _READ_ONLY_GIT:
                return False
            if words[1] == "branch" and _WRITE_GIT_BRANCH_FLAGS & set(words[2:]):
                return False
        elif program not in _READ_ONLY_PROGRAMS:
            return False
    return True


def _tool_name(name: str, tool_input: Any) -> str:
    if name == "Bash":
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        if isinstance(command, str) and _is_read_only_bash(command):
            return "Bash.read"
    return name


def _tools_list(names: set[str]) -> dict[str, Any]:
    tools: list[dict[str, Any]] = []
    for name in sorted(names):
        read_only = name in _READ_ONLY_TOOLS or name == "Bash.read"
        tools.append(
            {
                "name": name,
                "description": name,
                "inputSchema": {"type": "object"},
                "annotations": {
                    "readOnlyHint": read_only,
                    "destructiveHint": not read_only,
                    "idempotentHint": read_only,
                    "openWorldHint": not read_only,
                },
            }
        )
    return {"tools": tools}


class HandlerTraceConvert:
    """Map one run's log records onto invoke_agent, chat and execute_tool spans."""

    def handle(
        self, request: ModelAgentTraceConvertRequest
    ) -> ModelAgentTraceConvertResult:
        limit = request.max_text_chars
        trace_id = _sha_hex(request.lane, 32)
        clock = _BASE_NS
        first_time: int | None = None
        last_time = _BASE_NS
        session_id = ""
        first_user_text = ""
        final_text = ""
        chats: list[tuple[int, str]] = []
        calls: dict[str, dict[str, Any]] = {}
        call_order: list[str] = []
        results: dict[str, tuple[int, str, bool]] = {}

        for record in request.records:
            stamp = _timestamp_ns(record)
            clock = stamp if stamp is not None else clock + _STEP_NS
            if first_time is None:
                first_time = clock
            last_time = clock
            sid = record.get("sessionId")
            if not session_id and isinstance(sid, str) and sid:
                session_id = sid
            kind = record.get("type")
            message = record.get("message")
            if kind not in ("assistant", "user") or not isinstance(message, dict):
                continue
            content = message.get("content")
            if kind == "user":
                if isinstance(content, str):
                    if not first_user_text:
                        first_user_text = content
                    continue
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_result":
                        use_id = str(block.get("tool_use_id", ""))
                        results[use_id] = (
                            clock,
                            _block_text(block.get("content")),
                            bool(block.get("is_error")),
                        )
                    elif block.get("type") == "text" and not first_user_text:
                        first_user_text = str(block.get("text", ""))
                continue
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    text = str(block.get("text", ""))
                    chats.append((clock, text))
                    final_text = text
                elif block.get("type") == "tool_use":
                    use_id = str(block.get("id", ""))
                    if use_id not in calls:
                        call_order.append(use_id)
                    calls[use_id] = {
                        "name": str(block.get("name", "")),
                        "input": block.get("input", {}),
                        "t": clock,
                    }

        start = first_time if first_time is not None else _BASE_NS
        end = max(last_time, start + _STEP_NS)
        root_id = _sha_hex(f"{trace_id}/root", 16)
        spans: list[dict[str, Any]] = [
            {
                "traceId": trace_id,
                "spanId": root_id,
                "parentSpanId": "",
                "name": f"invoke_agent {request.lane}",
                "kind": _SPAN_KIND_INTERNAL,
                "startTimeUnixNano": str(start),
                "endTimeUnixNano": str(end),
                "attributes": [
                    _attr("gen_ai.operation.name", "invoke_agent"),
                    _attr("gen_ai.agent.name", request.lane),
                    _attr("gen_ai.conversation.id", session_id or request.lane),
                    _attr(
                        "gen_ai.input.messages",
                        _text_parts("user", _clip(first_user_text, limit)),
                    ),
                    _attr(
                        "gen_ai.output.messages",
                        _text_parts("assistant", _clip(final_text, limit)),
                    ),
                ],
            }
        ]
        index = 0
        for stamp, text in chats:
            index += 1
            spans.append(
                {
                    "traceId": trace_id,
                    "spanId": _sha_hex(f"{trace_id}/{index}", 16),
                    "parentSpanId": root_id,
                    "name": "chat",
                    "kind": _SPAN_KIND_INTERNAL,
                    "startTimeUnixNano": str(stamp),
                    "endTimeUnixNano": str(stamp + _STEP_NS),
                    "attributes": [
                        _attr("gen_ai.operation.name", "chat"),
                        _attr(
                            "gen_ai.output.messages",
                            _text_parts("assistant", _clip(text, limit)),
                        ),
                    ],
                }
            )
        tool_names: set[str] = set()
        unmatched: list[str] = []
        matched = 0
        for use_id in call_order:
            call = calls[use_id]
            index += 1
            name = _tool_name(call["name"], call["input"])
            tool_names.add(name)
            started = int(call["t"])
            attributes = [
                _attr("gen_ai.operation.name", "execute_tool"),
                _attr("gen_ai.tool.name", name),
                _attr("gen_ai.tool.type", "function"),
                _attr("gen_ai.tool.call.id", use_id),
                _attr(
                    "gen_ai.tool.call.arguments",
                    json.dumps(call["input"], sort_keys=True, ensure_ascii=False),
                ),
            ]
            span: dict[str, Any] = {
                "traceId": trace_id,
                "spanId": _sha_hex(f"{trace_id}/{index}", 16),
                "parentSpanId": root_id,
                "name": f"execute_tool {name}",
                "kind": _SPAN_KIND_INTERNAL,
                "startTimeUnixNano": str(started),
            }
            result = results.get(use_id)
            if result is None:
                unmatched.append(use_id)
                span["endTimeUnixNano"] = str(started + _STEP_NS)
            else:
                matched += 1
                finished, result_text, is_error = result
                span["endTimeUnixNano"] = str(max(finished, started + _STEP_NS))
                attributes.append(
                    _attr("gen_ai.tool.call.result", _clip(result_text, limit))
                )
                if is_error:
                    attributes.append(_attr("error.type", "tool_error"))
                    span["status"] = {"code": _STATUS_ERROR, "message": "tool error"}
            span["attributes"] = attributes
            spans.append(span)

        otlp = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [_attr("service.name", request.lane)],
                    },
                    "scopeSpans": [{"scope": {"name": _SCOPE_NAME}, "spans": spans}],
                }
            ]
        }
        return ModelAgentTraceConvertResult(
            otlp=otlp,
            tools=_tools_list(tool_names),
            tool_call_count=len(call_order),
            tool_result_count=matched,
            unmatched_tool_call_ids=tuple(unmatched),
            final_message=final_text,
        )
