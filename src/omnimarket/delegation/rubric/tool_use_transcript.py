# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Turn one recorded agentic run into the tool_use rubric request (OMN-20233).

Two recorders are read: a crush session (the ``messages`` rows of its sqlite
store, as the lab-run crush driver leaves them) and a Claude Code
``--output-format stream-json`` event stream (as harness-delegate keeps it).
Both become the same ``ModelToolUseTranscript``. Everything here is pure: the
caller reads the files and passes rows, events, tool schemas and the
workspace manifest in.

Caller normalisation the compute relies on (it compares paths as strings):
path arguments and answer citations that sit under ``workspace_root`` are
rewritten relative to it, so they meet the relative paths of the manifest.

Tool outputs lose their terminal colour codes, which split a printed
``5 passed in 0.55s`` so the answer quoting it could not be traced.

Execution evidence for ``stated_check_passes`` is taken only from what the
environment printed: the last shell call whose command names a test target,
read for a pytest summary line. Answer prose is never read for it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from omnimarket.nodes.node_delegation_rubric_check_compute.models import (
    EnumToolCallStatus,
    EnumToolParameterType,
    ModelClassRubric,
    ModelDeclaredTool,
    ModelNoPhantomPathsParams,
    ModelRubricCheckRequest,
    ModelRubricExecutionResult,
    ModelTestTargetsParams,
    ModelToolCall,
    ModelToolCallResult,
    ModelToolParameter,
    ModelToolUseTranscript,
    ModelWorkspaceFile,
)

TOOL_USE_CLASS = "tool_use"
SHELL_TOOLS = frozenset({"bash", "Bash", "shell", "exec_command"})
_PASSED = re.compile(r"\b\d+ passed\b")
_TEST_DIRECTORY = re.compile(r"(?<![\w/.-])tests(?:/[\w-]+)*/?(?![\w./-])")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_FAILED = re.compile(r"\b\d+ (?:failed|errors?)\b|\bno tests ran\b")
# Unix seconds before this bound; crush stores seconds although its schema says ms.
_MS_EPOCH_FLOOR = 100_000_000_000
_DEFAULT_PATH_ARGUMENTS = ("path", "file_path", "notebook_path")


@dataclass(frozen=True)
class CrushMessage:
    """One row of crush's ``messages`` table, in ``created_at`` order."""

    role: str
    parts_json: str
    created_at: int
    finished_at: int | None = None


def _json_type(schema: Mapping[str, object]) -> EnumToolParameterType:
    """The declared JSON type; a union takes its first non-null member, an untyped value is an object."""
    raw = schema.get("type")
    names = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
    for name in names:
        if isinstance(name, str) and name != "null":
            return EnumToolParameterType(name)
    if "null" in names:
        return EnumToolParameterType.NULL
    return EnumToolParameterType.OBJECT


def declared_tools_from_schemas(
    tools: Iterable[Mapping[str, object]],
) -> tuple[ModelDeclaredTool, ...]:
    """Tool schemas as a model was offered them: OpenAI ``function`` or Anthropic ``input_schema`` form."""
    declared: dict[str, ModelDeclaredTool] = {}
    for tool in tools:
        function = tool.get("function")
        if isinstance(function, Mapping):
            name, schema = function.get("name"), function.get("parameters")
        else:
            name, schema = tool.get("name"), tool.get("input_schema")
        if not isinstance(name, str) or not name:
            raise ValueError("a tool schema has no name")
        schema = schema if isinstance(schema, Mapping) else {}
        properties = schema.get("properties")
        properties = properties if isinstance(properties, Mapping) else {}
        required_raw = schema.get("required")
        required = (
            {str(item) for item in required_raw}
            if isinstance(required_raw, list)
            else set()
        )
        declared[name] = ModelDeclaredTool(
            name=name,
            parameters=tuple(
                ModelToolParameter(
                    name=str(param),
                    json_type=_json_type(spec if isinstance(spec, Mapping) else {}),
                    required=str(param) in required,
                )
                for param, spec in properties.items()
            ),
        )
    return tuple(declared.values())


def _relative(value: str, root: str | None) -> str:
    if not root:
        return value
    prefix = root.rstrip("/") + "/"
    if value == root.rstrip("/"):
        return "."
    return value[len(prefix) :] if value.startswith(prefix) else value


def _normalised_arguments(
    text: str, root: str | None, path_arguments: Sequence[str]
) -> str:
    """Path arguments under the workspace root, made relative; anything unparsable is kept verbatim."""
    if not root:
        return text
    try:
        value: object = json.loads(text)
    except (ValueError, RecursionError):
        return text
    if not isinstance(value, dict):
        return text
    changed = False
    for name in path_arguments:
        item = value.get(name)
        if isinstance(item, str):
            relative = _relative(item, root)
            if relative != item:
                value[name] = relative
                changed = True
    return json.dumps(value) if changed else text


def _path_arguments(rubric: ModelClassRubric) -> tuple[str, ...]:
    for row in rubric.criteria:
        if isinstance(row.params, ModelNoPhantomPathsParams):
            return tuple(row.params.path_argument_names)
    return _DEFAULT_PATH_ARGUMENTS


def _target_pattern(rubric: ModelClassRubric) -> str | None:
    for row in rubric.criteria:
        if row.criterion_id == "stated_check_passes" and isinstance(
            row.params, ModelTestTargetsParams
        ):
            return row.params.target_pattern
    return None


def execution_results_from_calls(
    calls: Sequence[ModelToolCall], target_pattern: str | None, answer_text: str = ""
) -> tuple[ModelRubricExecutionResult, ...]:
    """Test targets a shell call ran, with the pytest summary the environment printed; the last run wins.

    A target is one the command names, or a test file the answer names that
    lies under a ``tests`` directory the command ran whole.
    """
    if target_pattern is None:
        return ()
    claimed = [
        target
        for target in dict.fromkeys(re.findall(target_pattern, answer_text))
        if target.startswith("tests/")
    ]
    outcome: dict[str, bool] = {}
    for call in calls:
        if call.tool_name not in SHELL_TOOLS or call.result is None:
            continue
        try:
            arguments: object = json.loads(call.arguments_json)
        except (ValueError, RecursionError):
            continue
        command = arguments.get("command") if isinstance(arguments, dict) else None
        if not isinstance(command, str):
            continue
        # Only a printed summary is evidence. A refused or crashed call that printed none
        # (a permission prompt, a missing interpreter) ran no test and records nothing.
        output = call.result.output
        if _FAILED.search(output):
            passed = False
        elif _PASSED.search(output):
            passed = call.result.status == EnumToolCallStatus.OK
        else:
            continue
        for target in dict.fromkeys(re.findall(target_pattern, command)):
            outcome[target] = passed
        for directory in _TEST_DIRECTORY.findall(command):
            prefix = directory.rstrip("/") + "/"
            for target in claimed:
                if target.split("::", 1)[0].startswith(prefix):
                    outcome[target] = passed
    return tuple(
        ModelRubricExecutionResult(target=target, passed=passed)
        for target, passed in outcome.items()
    )


def _request(
    *,
    rubric: ModelClassRubric,
    request_text: str,
    answer_text: str,
    workspace_root: str | None,
    declared_tools: tuple[ModelDeclaredTool, ...],
    calls: list[ModelToolCall],
    turn_count: int,
    wall_time_ms: int | None,
    workspace_files: Sequence[ModelWorkspaceFile] | None,
    extra_execution_results: Sequence[ModelRubricExecutionResult],
) -> ModelRubricCheckRequest:
    path_arguments = _path_arguments(rubric)
    normalised = [
        call.model_copy(
            update={
                "arguments_json": _normalised_arguments(
                    call.arguments_json, workspace_root, path_arguments
                )
            }
        )
        for call in calls
    ]
    answer = answer_text
    if workspace_root:
        answer = answer.replace(workspace_root.rstrip("/") + "/", "")
    derived = execution_results_from_calls(normalised, _target_pattern(rubric), answer)
    supplied = {row.target for row in extra_execution_results}
    return ModelRubricCheckRequest(
        task_class=rubric.task_class,
        request_text=request_text,
        answer_text=answer,
        rubric=rubric,
        transcript=ModelToolUseTranscript(
            declared_tools=declared_tools,
            tool_calls=tuple(normalised),
            turn_count=turn_count,
            wall_time_ms=wall_time_ms,
            workspace_files=None if workspace_files is None else tuple(workspace_files),
        ),
        execution_results=(
            *extra_execution_results,
            *(row for row in derived if row.target not in supplied),
        ),
    )


def _ms(value: int) -> int:
    return value if value >= _MS_EPOCH_FLOOR else value * 1000


def _parts(row: CrushMessage) -> list[Mapping[str, object]]:
    try:
        value: object = json.loads(row.parts_json)
    except (ValueError, RecursionError):
        return []
    return (
        [part for part in value if isinstance(part, Mapping)]
        if isinstance(value, list)
        else []
    )


def _data(part: Mapping[str, object]) -> Mapping[str, object]:
    data = part.get("data")
    return data if isinstance(data, Mapping) else {}


def crush_request(
    messages: Sequence[CrushMessage],
    *,
    rubric: ModelClassRubric,
    declared_tools: tuple[ModelDeclaredTool, ...],
    workspace_root: str | None,
    workspace_files: Sequence[ModelWorkspaceFile] | None,
    request_text: str | None = None,
    execution_results: Sequence[ModelRubricExecutionResult] = (),
) -> ModelRubricCheckRequest:
    """One crush session. The request is its first user text unless the caller passes the task."""
    rows = sorted(messages, key=lambda row: row.created_at)
    user_texts: list[str] = []
    calls: dict[str, dict[str, object]] = {}
    order: list[str] = []
    results: dict[str, ModelToolCallResult] = {}
    answer = ""
    turns = 0
    for row in rows:
        parts = _parts(row)
        if row.role == "user":
            text = "\n".join(
                str(_data(part).get("text", ""))
                for part in parts
                if part.get("type") == "text"
            )
            if text:
                user_texts.append(text)
        elif row.role == "assistant":
            turns += 1
            texts = [
                str(_data(part).get("text", ""))
                for part in parts
                if part.get("type") == "text"
            ]
            if any(text.strip() for text in texts):
                answer = "\n".join(texts)
            for part in parts:
                if part.get("type") != "tool_call":
                    continue
                data = _data(part)
                call_id = str(data.get("id") or "")
                if call_id and call_id not in calls:
                    order.append(call_id)
                    calls[call_id] = {
                        "name": str(data.get("name") or ""),
                        "input": str(data.get("input") or ""),
                    }
        elif row.role == "tool":
            for part in parts:
                if part.get("type") != "tool_result":
                    continue
                data = _data(part)
                results[str(data.get("tool_call_id") or "")] = ModelToolCallResult(
                    status=EnumToolCallStatus.ERROR
                    if data.get("is_error") is True
                    else EnumToolCallStatus.OK,
                    output=_ANSI.sub("", str(data.get("content") or "")),
                )
    tool_calls = [
        ModelToolCall(
            call_id=call_id,
            tool_name=str(calls[call_id]["name"]) or "unnamed",
            arguments_json=str(calls[call_id]["input"]),
            result=results.get(call_id),
        )
        for call_id in order
    ]
    wall: int | None = None
    if rows:
        end = max(max(row.created_at, row.finished_at or 0) for row in rows)
        wall = max(0, _ms(end) - _ms(rows[0].created_at))
    return _request(
        rubric=rubric,
        request_text=request_text
        if request_text is not None
        else (user_texts[0] if user_texts else ""),
        answer_text=answer,
        workspace_root=workspace_root,
        declared_tools=declared_tools,
        calls=tool_calls,
        turn_count=turns,
        wall_time_ms=wall,
        workspace_files=workspace_files,
        extra_execution_results=execution_results,
    )


def _result_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text", ""))
            for item in content
            if isinstance(item, Mapping) and item.get("type") == "text"
        )
    return ""


def claude_stream_request(
    events: Sequence[Mapping[str, object]],
    *,
    rubric: ModelClassRubric,
    request_text: str,
    declared_tools: tuple[ModelDeclaredTool, ...],
    workspace_root: str | None,
    workspace_files: Sequence[ModelWorkspaceFile] | None,
    execution_results: Sequence[ModelRubricExecutionResult] = (),
) -> ModelRubricCheckRequest:
    """One Claude Code ``stream-json`` run (``--verbose``); the final answer is the result event's text."""
    calls: dict[str, ModelToolCall] = {}
    order: list[str] = []
    results: dict[str, ModelToolCallResult] = {}
    message_ids: list[str] = []
    answer = ""
    num_turns: int | None = None
    duration: int | None = None
    for event in events:
        kind = event.get("type")
        message = event.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        blocks = (
            [block for block in content if isinstance(block, Mapping)]
            if isinstance(content, list)
            else []
        )
        if kind == "assistant" and isinstance(message, Mapping):
            message_id = str(message.get("id") or len(message_ids))
            if message_id not in message_ids:
                message_ids.append(message_id)
            for block in blocks:
                if block.get("type") != "tool_use":
                    continue
                call_id = str(block.get("id") or "")
                if call_id and call_id not in calls:
                    order.append(call_id)
                    calls[call_id] = ModelToolCall(
                        call_id=call_id,
                        tool_name=str(block.get("name") or "unnamed"),
                        arguments_json=json.dumps(block.get("input", {})),
                    )
        elif kind == "user":
            for block in blocks:
                if block.get("type") != "tool_result":
                    continue
                results[str(block.get("tool_use_id") or "")] = ModelToolCallResult(
                    status=EnumToolCallStatus.ERROR
                    if block.get("is_error") is True
                    else EnumToolCallStatus.OK,
                    output=_ANSI.sub("", _result_text(block.get("content"))),
                )
        elif kind == "result":
            answer = str(event.get("result") or "")
            turns_raw = event.get("num_turns")
            num_turns = (
                turns_raw if isinstance(turns_raw, int) and turns_raw >= 0 else None
            )
            duration_raw = event.get("duration_ms")
            duration = (
                duration_raw
                if isinstance(duration_raw, int) and duration_raw >= 0
                else None
            )
    tool_calls = [
        calls[call_id].model_copy(update={"result": results.get(call_id)})
        for call_id in order
    ]
    return _request(
        rubric=rubric,
        request_text=request_text,
        answer_text=answer,
        workspace_root=workspace_root,
        declared_tools=declared_tools,
        calls=tool_calls,
        turn_count=num_turns if num_turns is not None else len(message_ids),
        wall_time_ms=duration,
        workspace_files=workspace_files,
        extra_execution_results=execution_results,
    )
