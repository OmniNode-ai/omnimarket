# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24941: a headless agent run's log becomes an OpenTelemetry GenAI trace."""

from __future__ import annotations

import json
from typing import Any

import pytest

from omnimarket.models.model_agent_trace_otel import (
    ModelAgentTraceConvertRequest,
    ModelAgentTraceConvertResult,
)
from omnimarket.nodes.node_agent_trace_otel_compute.handlers.handler_trace_convert import (
    HandlerTraceConvert,
)

pytestmark = pytest.mark.unit


def _user_text(text: str, stamp: str) -> dict[str, Any]:
    return {
        "type": "user",
        "timestamp": stamp,
        "sessionId": "sess-1",
        "message": {"role": "user", "content": text},
    }


def _assistant(blocks: list[dict[str, Any]], stamp: str) -> dict[str, Any]:
    return {
        "type": "assistant",
        "timestamp": stamp,
        "message": {"role": "assistant", "content": blocks},
    }


def _tool_use(use_id: str, name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    return {"type": "tool_use", "id": use_id, "name": name, "input": tool_input}


def _tool_result(
    use_id: str, content: Any, stamp: str, is_error: bool = False
) -> dict[str, Any]:
    return {
        "type": "user",
        "timestamp": stamp,
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": use_id,
                    "content": content,
                    "is_error": is_error,
                }
            ],
        },
    }


def _convert(
    records: list[dict[str, Any]], max_text_chars: int = 20_000
) -> ModelAgentTraceConvertResult:
    return HandlerTraceConvert().handle(
        ModelAgentTraceConvertRequest(
            lane="lane-x", records=tuple(records), max_text_chars=max_text_chars
        )
    )


def _spans(result: ModelAgentTraceConvertResult) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = result.otlp["resourceSpans"][0]["scopeSpans"][0][
        "spans"
    ]
    return spans


def _attrs(span: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in span["attributes"]:
        out[item["key"]] = next(iter(item["value"].values()))
    return out


def _tool_spans(result: ModelAgentTraceConvertResult) -> list[dict[str, Any]]:
    return [s for s in _spans(result) if s["name"].startswith("execute_tool")]


def _basic_run() -> list[dict[str, Any]]:
    return [
        {
            "type": "queue-operation",
            "operation": "dequeue",
            "timestamp": "2026-11-16T10:00:00Z",
        },
        _user_text("fix the unit", "2026-11-16T10:00:01Z"),
        _assistant(
            [
                {"type": "thinking", "thinking": "hmm"},
                {"type": "text", "text": "checking the unit"},
                _tool_use("t1", "Bash", {"command": "systemctl status x"}),
            ],
            "2026-11-16T10:00:02Z",
        ),
        _tool_result("t1", "active (running)", "2026-11-16T10:00:05Z"),
        _assistant(
            [{"type": "text", "text": "done: the unit is running"}],
            "2026-11-16T10:00:06Z",
        ),
    ]


def test_tool_result_is_mapped() -> None:
    result = _convert(_basic_run())
    (span,) = _tool_spans(result)
    attrs = _attrs(span)
    assert attrs["gen_ai.tool.call.result"] == "active (running)"
    assert attrs["gen_ai.tool.name"] == "Bash"
    assert attrs["gen_ai.tool.call.id"] == "t1"
    assert json.loads(attrs["gen_ai.tool.call.arguments"]) == {
        "command": "systemctl status x"
    }
    assert result.tool_call_count == 1
    assert result.tool_result_count == 1
    assert (
        int(span["endTimeUnixNano"]) - int(span["startTimeUnixNano"]) == 3_000_000_000
    )


def test_result_text_blocks_are_joined() -> None:
    records = [
        _user_text("go", "2026-11-16T10:00:00Z"),
        _assistant(
            [_tool_use("t1", "Read", {"file_path": "/x"})], "2026-11-16T10:00:01Z"
        ),
        _tool_result(
            "t1",
            [
                {"type": "text", "text": "line one"},
                {"type": "text", "text": "line two"},
            ],
            "2026-11-16T10:00:02Z",
        ),
    ]
    (span,) = _tool_spans(_convert(records))
    assert _attrs(span)["gen_ai.tool.call.result"] == "line one\nline two"


def test_error_result_marks_span() -> None:
    records = [
        _user_text("go", "2026-11-16T10:00:00Z"),
        _assistant(
            [_tool_use("t1", "Bash", {"command": "make"})], "2026-11-16T10:00:01Z"
        ),
        _tool_result("t1", "boom", "2026-11-16T10:00:02Z", is_error=True),
    ]
    (span,) = _tool_spans(_convert(records))
    assert _attrs(span)["error.type"] == "tool_error"
    assert span["status"]["code"] == 2


def test_success_result_has_no_error_marker() -> None:
    (span,) = _tool_spans(_convert(_basic_run()))
    assert "error.type" not in _attrs(span)
    assert "status" not in span


def test_final_message_is_last_assistant_text() -> None:
    result = _convert(_basic_run())
    assert result.final_message == "done: the unit is running"
    root = _spans(result)[0]
    output = json.loads(_attrs(root)["gen_ai.output.messages"])
    assert output[0]["role"] == "assistant"
    assert output[0]["parts"][0]["content"] == "done: the unit is running"


def test_root_span_input_is_first_user_text() -> None:
    root = _spans(_convert(_basic_run()))[0]
    attrs = _attrs(root)
    assert root["name"] == "invoke_agent lane-x"
    assert root["parentSpanId"] == ""
    assert attrs["gen_ai.conversation.id"] == "sess-1"
    first = json.loads(attrs["gen_ai.input.messages"])
    assert first[0]["parts"][0]["content"] == "fix the unit"


def test_one_chat_span_per_assistant_text_block() -> None:
    chats = [s for s in _spans(_convert(_basic_run())) if s["name"] == "chat"]
    assert len(chats) == 2


def test_unmatched_tool_call_listed() -> None:
    records = [
        _user_text("go", "2026-11-16T10:00:00Z"),
        _assistant(
            [
                _tool_use("t1", "Bash", {"command": "make"}),
                _tool_use("t2", "Bash", {"command": "make test"}),
            ],
            "2026-11-16T10:00:01Z",
        ),
        _tool_result("t1", "ok", "2026-11-16T10:00:02Z"),
    ]
    result = _convert(records)
    assert result.tool_call_count == 2
    assert result.tool_result_count == 1
    assert result.unmatched_tool_call_ids == ("t2",)
    unmatched = [
        s for s in _tool_spans(result) if "gen_ai.tool.call.result" not in _attrs(s)
    ]
    assert len(unmatched) == 1


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("ls -la | grep x", "Bash.read"),
        ("git log --oneline", "Bash.read"),
        ("sed -n 1,5p file", "Bash.read"),
        ("echo hi > f", "Bash"),
        ("rm -rf x", "Bash"),
        ("ssh host systemctl restart x", "Bash"),
        ("sed -i s/a/b/ f", "Bash"),
        ("git push origin main", "Bash"),
        ("git branch -D old", "Bash"),
        ("ls && rm x", "Bash"),
    ],
)
def test_bash_read_classification(command: str, expected: str) -> None:
    records = [
        _user_text("go", "2026-11-16T10:00:00Z"),
        _assistant(
            [_tool_use("t1", "Bash", {"command": command})], "2026-11-16T10:00:01Z"
        ),
        _tool_result("t1", "x", "2026-11-16T10:00:02Z"),
    ]
    (span,) = _tool_spans(_convert(records))
    assert _attrs(span)["gen_ai.tool.name"] == expected


def test_tools_list_marks_reads_and_writes() -> None:
    records = [
        _user_text("go", "2026-11-16T10:00:00Z"),
        _assistant(
            [
                _tool_use("t1", "Read", {"file_path": "/x"}),
                _tool_use("t2", "Bash", {"command": "rm x"}),
                _tool_use("t3", "Bash", {"command": "ls"}),
            ],
            "2026-11-16T10:00:01Z",
        ),
    ]
    tools = {t["name"]: t for t in _convert(records).tools["tools"]}
    assert sorted(tools) == ["Bash", "Bash.read", "Read"]
    assert tools["Read"]["annotations"]["readOnlyHint"] is True
    assert tools["Bash.read"]["annotations"]["readOnlyHint"] is True
    assert tools["Bash"]["annotations"]["readOnlyHint"] is False
    assert tools["Bash"]["annotations"]["destructiveHint"] is True


def test_ignored_record_types_produce_no_spans() -> None:
    records: list[dict[str, Any]] = [
        {"type": "queue-operation", "operation": "dequeue"},
        {"type": "attachment", "timestamp": "2026-11-16T10:00:00Z"},
        {"type": "cost-state", "totalCostUSD": 1.0},
    ]
    result = _convert(records)
    assert len(_spans(result)) == 1
    assert result.tool_call_count == 0
    assert result.final_message == ""


def test_conversion_is_deterministic() -> None:
    assert _convert(_basic_run()) == _convert(_basic_run())


def test_missing_timestamps_get_monotonic_times() -> None:
    records = [
        {"type": "user", "message": {"role": "user", "content": "go"}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [_tool_use("t1", "Bash", {"command": "ls"})],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "x"}
                ],
            },
        },
    ]
    (span,) = _tool_spans(_convert(records))
    assert int(span["endTimeUnixNano"]) > int(span["startTimeUnixNano"])


def test_long_text_is_truncated() -> None:
    records = [
        _user_text("go", "2026-11-16T10:00:00Z"),
        _assistant(
            [_tool_use("t1", "Bash", {"command": "cat big"})], "2026-11-16T10:00:01Z"
        ),
        _tool_result("t1", "x" * 500, "2026-11-16T10:00:02Z"),
    ]
    (span,) = _tool_spans(_convert(records, max_text_chars=100))
    assert _attrs(span)["gen_ai.tool.call.result"] == "x" * 100
