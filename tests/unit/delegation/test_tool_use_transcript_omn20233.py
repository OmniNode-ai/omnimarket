# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tool_use caller: recorded crush and Claude Code runs become rubric requests (OMN-20233).

Synthetic transcripts only, shaped like the two recorders' real output.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from omnimarket.delegation.rubric.contract_loader import (
    load_delegation_class_rubrics,
)
from omnimarket.delegation.rubric.tool_use_score import main
from omnimarket.delegation.rubric.tool_use_transcript import (
    CrushMessage,
    claude_stream_request,
    crush_request,
    declared_tools_from_schemas,
    execution_results_from_calls,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.handlers.handler_delegation_rubric_check import (
    HandlerDelegationRubricCheck,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.models import (
    EnumRubricOutcome,
    EnumToolCallStatus,
    EnumToolParameterType,
    ModelToolCall,
    ModelToolCallResult,
    ModelWorkspaceFile,
)

pytestmark = pytest.mark.unit

ROOT = "/work/tree"
RUBRIC = load_delegation_class_rubrics().for_class("tool_use")
CRUSH_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "view",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "offset": {"type": "integer"},
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                },
                "required": ["file_path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
]
MANIFEST = (
    ModelWorkspaceFile(path="src/pkg/mod.py", line_count=40),
    ModelWorkspaceFile(path="tests/unit/test_mod.py", line_count=20),
)


def _call(call_id: str, name: str, arguments: dict[str, object]) -> dict[str, object]:
    return {
        "type": "tool_call",
        "data": {
            "id": call_id,
            "name": name,
            "input": json.dumps(arguments),
            "finished": True,
        },
    }


def _result(
    call_id: str, name: str, content: str, is_error: bool = False
) -> dict[str, object]:
    return {
        "type": "tool_result",
        "data": {
            "tool_call_id": call_id,
            "name": name,
            "content": content,
            "is_error": is_error,
        },
    }


def _row(role: str, parts: list[dict[str, object]], at: int) -> CrushMessage:
    return CrushMessage(
        role=role, parts_json=json.dumps(parts), created_at=at, finished_at=at
    )


def crush_rows(
    answer: str, view_path: str = f"{ROOT}/src/pkg/mod.py"
) -> list[CrushMessage]:
    return [
        _row(
            "user",
            [{"type": "text", "data": {"text": "Fix the bug in src/pkg/mod.py."}}],
            1_790_000_000,
        ),
        _row(
            "assistant", [_call("c1", "view", {"file_path": view_path})], 1_790_000_001
        ),
        _row("tool", [_result("c1", "view", "     1|def f():\n")], 1_790_000_002),
        _row(
            "assistant",
            [
                _call(
                    "c2",
                    "edit",
                    {
                        "file_path": f"{ROOT}/src/pkg/mod.py",
                        "old_string": "a",
                        "new_string": "b",
                    },
                ),
                _call("c3", "bash", {"command": "uv run pytest tests/unit -q"}),
            ],
            1_790_000_010,
        ),
        _row(
            "tool",
            [
                _result("c2", "edit", "Content replaced"),
                _result(
                    "c3", "bash", "tests/unit/test_mod.py ....\n4 passed in 0.10s\n"
                ),
            ],
            1_790_000_020,
        ),
        _row("assistant", [{"type": "text", "data": {"text": answer}}], 1_790_000_030),
    ]


def verdict_of(request):  # type: ignore[no-untyped-def]
    return HandlerDelegationRubricCheck().handle(request)


def outcomes(request) -> dict[str, str]:  # type: ignore[no-untyped-def]
    return {row.criterion_id: row.outcome.value for row in verdict_of(request).criteria}


def test_declared_tools_read_openai_and_anthropic_forms() -> None:
    tools = declared_tools_from_schemas(
        [
            *CRUSH_TOOLS[:1],
            {
                "name": "Read",
                "input_schema": {
                    "properties": {
                        "file_path": {"type": "string"},
                        "limit": {"type": ["integer", "null"]},
                        "anything": {"description": "untyped"},
                    },
                    "required": ["file_path"],
                },
            },
        ]
    )
    read = {tool.name: tool for tool in tools}["Read"]
    params = {param.name: param for param in read.parameters}
    assert params["file_path"].required is True
    assert params["limit"].json_type == EnumToolParameterType.INTEGER
    assert params["anything"].json_type == EnumToolParameterType.OBJECT
    assert {tool.name for tool in tools} == {"view", "Read"}


def test_crush_run_passes_and_paths_are_made_relative() -> None:
    request = crush_request(
        crush_rows("Fixed src/pkg/mod.py:1 and tests/unit/test_mod.py passes."),
        rubric=RUBRIC,
        declared_tools=declared_tools_from_schemas(CRUSH_TOOLS),
        workspace_root=ROOT,
        workspace_files=MANIFEST,
    )
    transcript = request.transcript
    assert transcript is not None
    assert json.loads(transcript.tool_calls[0].arguments_json) == {
        "file_path": "src/pkg/mod.py"
    }
    assert transcript.turn_count == 3
    assert transcript.wall_time_ms == 30_000
    assert request.request_text == "Fix the bug in src/pkg/mod.py."
    assert [(row.target, row.passed) for row in request.execution_results] == [
        ("tests/unit/test_mod.py", True)
    ]
    assert outcomes(request) == {
        "tool_calls_wellformed": "PASS",
        "no_phantom_paths": "PASS",
        "edits_apply": "PASS",
        "stated_check_passes": "PASS",
        "task_answer_traceable": "PASS",
        "within_budget": "PASS",
    }


def test_crush_phantom_path_and_unknown_argument_fail() -> None:
    rows = crush_rows("Done.", view_path=f"{ROOT}/src/pkg/missing.py")
    rows[1] = _row(
        "assistant",
        [_call("c1", "view", {"path": f"{ROOT}/src/pkg/missing.py"})],
        1_790_000_001,
    )
    request = crush_request(
        rows,
        rubric=RUBRIC,
        declared_tools=declared_tools_from_schemas(CRUSH_TOOLS),
        workspace_root=ROOT,
        workspace_files=MANIFEST,
    )
    verdict = verdict_of(request)
    assert verdict.outcome == EnumRubricOutcome.FAIL
    assert set(verdict.failed_criteria) >= {"tool_calls_wellformed", "no_phantom_paths"}


def test_crush_undeclared_tool_and_failed_edit_fail() -> None:
    rows = crush_rows("Done.")
    rows[1] = _row(
        "assistant",
        [_call("c1", "read_file", {"path": "src/pkg/mod.py"})],
        1_790_000_001,
    )
    rows[2] = _row(
        "tool",
        [_result("c1", "read_file", "tool not found: read_file", True)],
        1_790_000_002,
    )
    rows[4] = _row(
        "tool",
        [
            _result("c2", "edit", "old_string not found", True),
            _result("c3", "bash", "4 passed"),
        ],
        1_790_000_020,
    )
    request = crush_request(
        rows,
        rubric=RUBRIC,
        declared_tools=declared_tools_from_schemas(CRUSH_TOOLS),
        workspace_root=ROOT,
        workspace_files=MANIFEST,
    )
    failed = {
        row.criterion_id: row.reason_code
        for row in verdict_of(request).criteria
        if row.outcome == EnumRubricOutcome.FAIL
    }
    assert failed["tool_calls_wellformed"] == "undeclared_tool"
    assert failed["edits_apply"] == "edit_failed"


def test_no_manifest_leaves_paths_undetermined_and_no_answer_is_kept_empty() -> None:
    rows = crush_rows("unused")[:-1]
    request = crush_request(
        rows,
        rubric=RUBRIC,
        declared_tools=declared_tools_from_schemas(CRUSH_TOOLS),
        workspace_root=ROOT,
        workspace_files=None,
    )
    assert request.answer_text == ""
    assert outcomes(request)["no_phantom_paths"] == "UNDETERMINED"


def test_millisecond_timestamps_are_not_scaled() -> None:
    rows = [
        _row("user", [{"type": "text", "data": {"text": "x"}}], 1_790_000_000_000),
        _row("assistant", [{"type": "text", "data": {"text": "y"}}], 1_790_000_005_000),
    ]
    request = crush_request(
        rows, rubric=RUBRIC, declared_tools=(), workspace_root=None, workspace_files=()
    )
    assert request.transcript is not None
    assert request.transcript.wall_time_ms == 5_000


@pytest.mark.parametrize(
    ("output", "status", "expected"),
    [
        ("12 passed in 1s", EnumToolCallStatus.OK, True),
        ("1 failed, 11 passed", EnumToolCallStatus.OK, False),
        ("2 errors", EnumToolCallStatus.OK, False),
        ("12 passed", EnumToolCallStatus.ERROR, False),
    ],
)
def test_execution_results_read_the_printed_summary(
    output: str, status: EnumToolCallStatus, expected: bool
) -> None:
    call = ModelToolCall(
        call_id="c",
        tool_name="Bash",
        arguments_json=json.dumps({"command": "pytest tests/unit/test_mod.py::test_a"}),
        result=ModelToolCallResult(status=status, output=output),
    )
    rows = execution_results_from_calls(
        [call], r"(?<![\w/])tests/[\w./-]+\.py(?:::[\w.\[\]-]+)*"
    )
    assert [(row.target, row.passed) for row in rows] == [
        ("tests/unit/test_mod.py::test_a", expected)
    ]


def test_a_refused_shell_call_records_no_result() -> None:
    call = ModelToolCall(
        call_id="c",
        tool_name="Bash",
        arguments_json=json.dumps(
            {"command": "uv run pytest tests/unit/test_mod.py -q"}
        ),
        result=ModelToolCallResult(
            status=EnumToolCallStatus.ERROR, output="This command requires approval"
        ),
    )
    assert execution_results_from_calls([call], r"tests/[\w./-]+\.py") == ()


def test_execution_results_ignore_output_without_a_summary() -> None:
    call = ModelToolCall(
        call_id="c",
        tool_name="bash",
        arguments_json=json.dumps({"command": "cat tests/unit/test_mod.py"}),
        result=ModelToolCallResult(
            status=EnumToolCallStatus.OK, output="def test_a(): ..."
        ),
    )
    assert execution_results_from_calls([call], r"tests/[\w./-]+\.py") == ()


def claude_events(answer: str) -> list[dict[str, object]]:
    return [
        {"type": "system", "subtype": "init", "tools": ["Read", "Grep"]},
        {
            "type": "assistant",
            "message": {
                "id": "m1",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Read",
                        "input": {"file_path": f"{ROOT}/src/pkg/mod.py"},
                    }
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": [{"type": "text", "text": "def f(): return 42"}],
                    }
                ]
            },
        },
        {
            "type": "assistant",
            "message": {"id": "m2", "content": [{"type": "text", "text": answer}]},
        },
        {
            "type": "result",
            "result": answer,
            "num_turns": 2,
            "duration_ms": 4200,
            "is_error": False,
        },
    ]


CLAUDE_TOOLS = [
    {
        "name": "Read",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "limit": {"type": "number"},
            },
            "required": ["file_path"],
        },
    }
]


def test_claude_stream_run_scores() -> None:
    request = claude_stream_request(
        claude_events("`f` in src/pkg/mod.py:1 returns 42."),
        rubric=RUBRIC,
        request_text="What does f return?",
        declared_tools=declared_tools_from_schemas(CLAUDE_TOOLS),
        workspace_root=ROOT,
        workspace_files=MANIFEST,
    )
    transcript = request.transcript
    assert transcript is not None
    assert transcript.turn_count == 2
    assert transcript.wall_time_ms == 4200
    assert transcript.tool_calls[0].result is not None
    assert transcript.tool_calls[0].result.output == "def f(): return 42"
    result = outcomes(request)
    assert result["tool_calls_wellformed"] == "PASS"
    assert result["no_phantom_paths"] == "PASS"
    assert result["within_budget"] == "PASS"


def test_claude_stream_invented_number_fails_traceability() -> None:
    request = claude_stream_request(
        claude_events("`f` returns 4711."),
        rubric=RUBRIC,
        request_text="What does f return?",
        declared_tools=declared_tools_from_schemas(CLAUDE_TOOLS),
        workspace_root=ROOT,
        workspace_files=MANIFEST,
    )
    assert "task_answer_traceable" in verdict_of(request).failed_criteria


def _crush_db(path: Path, rows: list[CrushMessage]) -> None:
    con = sqlite3.connect(path)
    con.execute(
        "create table sessions (id text primary key, parent_session_id text, created_at integer)"
    )
    con.execute(
        "create table messages (id text, session_id text, role text, parts text, "
        "created_at integer, finished_at integer)"
    )
    con.execute("insert into sessions values ('s1', null, 1790000000)")
    for index, row in enumerate(rows):
        con.execute(
            "insert into messages values (?, 's1', ?, ?, ?, ?)",
            (f"m{index}", row.role, row.parts_json, row.created_at, row.finished_at),
        )
    con.commit()
    con.close()


def test_cli_scores_a_crush_store_and_writes_the_record(tmp_path: Path) -> None:
    db = tmp_path / "crush.db"
    _crush_db(db, crush_rows("Fixed src/pkg/mod.py:1."))
    tools = tmp_path / "tools.json"
    tools.write_text(json.dumps(CRUSH_TOOLS))
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps([row.model_dump() for row in MANIFEST]))
    out = tmp_path / "rubric_verdict.json"
    assert (
        main(
            [
                "crush",
                "--db",
                str(db),
                "--declared-tools",
                str(tools),
                "--workspace-root",
                ROOT,
                "--workspace-manifest",
                str(manifest),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    record = json.loads(out.read_text())
    assert record["schema"] == "tool-use-rubric-verdict.v1"
    assert record["run_ref"] == "crush:s1"
    assert record["verdict"]["outcome"] == "PASS"
    assert record["attempt_verdict"]["task_class"] == "tool_use"
    assert record["measured"] == {"turns": 3, "tool_calls": 3, "wall_time_ms": 30_000}


def test_cli_refuses_a_missing_store(tmp_path: Path) -> None:
    tools = tmp_path / "tools.json"
    tools.write_text("[]")
    assert (
        main(
            ["crush", "--db", str(tmp_path / "none.db"), "--declared-tools", str(tools)]
        )
        == 2
    )


def test_terminal_colour_codes_are_stripped_from_tool_output() -> None:
    rows = crush_rows("Pytest result: `4 passed in 0.10s`.")
    rows[4] = _row(
        "tool",
        [
            _result("c2", "edit", "Content replaced"),
            _result(
                "c3", "bash", "\x1b[32m\x1b[1m4 passed\x1b[0m\x1b[32m in 0.10s\x1b[0m"
            ),
        ],
        1_790_000_020,
    )
    request = crush_request(
        rows,
        rubric=RUBRIC,
        declared_tools=declared_tools_from_schemas(CRUSH_TOOLS),
        workspace_root=ROOT,
        workspace_files=MANIFEST,
    )
    assert request.transcript is not None
    assert request.transcript.tool_calls[2].result is not None
    assert request.transcript.tool_calls[2].result.output == "4 passed in 0.10s"
    assert outcomes(request)["task_answer_traceable"] == "PASS"
