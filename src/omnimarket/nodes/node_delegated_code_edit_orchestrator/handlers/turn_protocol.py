# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The turn protocol of the delegated code edit loop (OMN-20290): pure.

* ``RESPONSE_CONTRACT`` is the JSON Schema every turn's ``onex delegate`` run
  declares, so the delegation quality gate validates the reply's shape and
  nothing else (OMN-15193: a declared contract is the gate's sole authority).
* ``TOOL_SCHEMAS`` are the tools as a model is offered them (OpenAI function
  form). The tool_use rubric reads the same schemas to judge each call.
* ``parse_turn_reply`` turns the reply text into typed actions, or names why it
  cannot.
* ``build_turn_prompt`` renders one turn's prompt from the request, the file
  index and the history. Same inputs, same bytes.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence

from pydantic import ValidationError

from omnimarket.nodes.node_delegated_code_edit_orchestrator.models.model_delegated_code_edit import (
    MAX_ACTIONS_PER_TURN,
    EnumCodeEditTool,
    ModelCodeEditAction,
    ModelDelegatedCodeEditRequest,
)

#: Fewest characters of a turn worth showing cut; below it the turn is left out.
_MIN_TURN_CHARS = 400

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)

#: Required string arguments per tool.
REQUIRED_ARGUMENTS: dict[EnumCodeEditTool, tuple[str, ...]] = {
    EnumCodeEditTool.VIEW: ("path",),
    EnumCodeEditTool.LS: (),
    EnumCodeEditTool.GREP: ("pattern",),
    EnumCodeEditTool.WRITE: ("file_path",),
    EnumCodeEditTool.EDIT: ("file_path", "old_string"),
    EnumCodeEditTool.FORMAT: ("file_path",),
    EnumCodeEditTool.RUN_CHECK: ("name",),
    EnumCodeEditTool.FINISH: (),
}

#: Arguments each tool accepts (the rest must be absent).
ALLOWED_ARGUMENTS: dict[EnumCodeEditTool, tuple[str, ...]] = {
    EnumCodeEditTool.VIEW: ("path", "offset"),
    EnumCodeEditTool.LS: ("path",),
    EnumCodeEditTool.GREP: ("pattern", "path"),
    EnumCodeEditTool.WRITE: ("file_path", "content"),
    EnumCodeEditTool.EDIT: ("file_path", "old_string", "new_string"),
    EnumCodeEditTool.FORMAT: ("file_path",),
    EnumCodeEditTool.RUN_CHECK: ("name",),
    EnumCodeEditTool.FINISH: ("summary",),
}

#: Arguments that are integers; every other argument is a string.
INTEGER_ARGUMENTS = frozenset({"offset"})

_DESCRIPTIONS: dict[EnumCodeEditTool, str] = {
    EnumCodeEditTool.VIEW: "Show one worktree file with line numbers, 250 lines "
    "at a time; offset (an integer) is the first line to show.",
    EnumCodeEditTool.LS: "List one worktree directory (default: the root).",
    EnumCodeEditTool.GREP: "Search worktree files for a regular expression.",
    EnumCodeEditTool.WRITE: "Create or replace one writable file with content.",
    EnumCodeEditTool.EDIT: "Replace old_string, which must occur exactly once, "
    "with new_string in one writable file.",
    EnumCodeEditTool.FORMAT: "Run the declared formatter over one writable file, "
    "rewriting it in place. Use it instead of hand-formatting.",
    EnumCodeEditTool.RUN_CHECK: "Run one declared check by name.",
    EnumCodeEditTool.FINISH: "Declare the task done; every declared check then runs.",
}

TOOL_SCHEMAS: tuple[dict[str, object], ...] = tuple(
    {
        "type": "function",
        "function": {
            "name": tool.value,
            "description": _DESCRIPTIONS[tool],
            "parameters": {
                "type": "object",
                "properties": {
                    name: {"type": "integer" if name in INTEGER_ARGUMENTS else "string"}
                    for name in ALLOWED_ARGUMENTS[tool]
                },
                "required": list(REQUIRED_ARGUMENTS[tool]),
            },
        },
    }
    for tool in EnumCodeEditTool
)

RESPONSE_CONTRACT: dict[str, object] = {
    "type": "object",
    "required": ["actions"],
    "properties": {
        "note": {"type": "string"},
        "actions": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_ACTIONS_PER_TURN,
            "items": {
                "type": "object",
                "required": ["tool"],
                "properties": {
                    "tool": {"enum": [tool.value for tool in EnumCodeEditTool]},
                    "path": {"type": "string"},
                    "offset": {"type": "integer", "minimum": 1},
                    "file_path": {"type": "string"},
                    "pattern": {"type": "string"},
                    "content": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                    "name": {"type": "string"},
                    "summary": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
}


def _candidates(text: str) -> list[str]:
    return [m.group(1) for m in _FENCE.finditer(text)] + [text]


def parse_turn_reply(text: str) -> tuple[tuple[ModelCodeEditAction, ...], str]:
    """(actions, invalid_reason) from one turn's reply text."""
    data: object = None
    for candidate in _candidates(text):
        start, end = candidate.find("{"), candidate.rfind("}")
        if start < 0 or end <= start:
            continue
        try:
            data = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            break
        data = None
    if not isinstance(data, dict):
        return (), "the reply is not a JSON object with an actions list"
    raw_actions = data.get("actions")
    if not isinstance(raw_actions, list) or not raw_actions:
        return (), "the JSON object has no non-empty actions list"
    if len(raw_actions) > MAX_ACTIONS_PER_TURN:
        return (), f"a turn carries at most {MAX_ACTIONS_PER_TURN} actions"
    actions: list[ModelCodeEditAction] = []
    for index, raw in enumerate(raw_actions, start=1):
        if not isinstance(raw, dict):
            return (), f"action {index} is not an object"
        try:
            tool = EnumCodeEditTool(str(raw.get("tool")))
        except ValueError:
            return (), f"action {index} names unknown tool {raw.get('tool')!r}"
        extra = sorted(set(raw) - {"tool", *ALLOWED_ARGUMENTS[tool]})
        if extra:
            return (), f"action {index} ({tool.value}) takes no {', '.join(extra)}"
        for name in REQUIRED_ARGUMENTS[tool]:
            value = raw.get(name)
            if not isinstance(value, str) or (name != "content" and not value):
                return (), f"action {index} ({tool.value}) needs a string {name}"
        if tool == EnumCodeEditTool.WRITE and not isinstance(
            raw.get("content", ""), str
        ):
            return (), f"action {index} (write) content is not a string"
        if "offset" in raw:
            offset = raw["offset"]
            if isinstance(offset, str) and offset.isdigit():
                offset = int(offset)
            if not isinstance(offset, int) or isinstance(offset, bool) or offset < 1:
                return (), f"action {index} (view) offset must be an integer >= 1"
            raw = {**raw, "offset": offset}
        try:
            actions.append(
                ModelCodeEditAction.model_validate(
                    {k: v for k, v in raw.items() if v is not None}
                )
            )
        except ValidationError as exc:
            return (), f"action {index} is malformed: {exc.errors()[0]['msg']}"
    return tuple(actions), ""


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [{len(text) - limit} more characters cut]\n"


def build_turn_prompt(
    request: ModelDelegatedCodeEditRequest,
    file_index: Sequence[str],
    context: Sequence[tuple[str, str]],
    history: Sequence[str],
    turn: int,
    *,
    max_chars: int = 90_000,
) -> str:
    """One turn's prompt. ``history`` holds the rendered previous turns, oldest first."""
    checks = "\n".join(f"- {c.name}: {' '.join(c.argv)}" for c in request.checks)
    globs = ", ".join(request.writable_globs)
    tools = "\n".join(
        f"- {tool.value}({', '.join(ALLOWED_ARGUMENTS[tool])}): {_DESCRIPTIONS[tool]}"
        for tool in EnumCodeEditTool
    )
    head = (
        "You are editing a git worktree to complete one task. You act only "
        "through tools. Reply with ONE JSON object and nothing else:\n"
        '{"note": "<one line>", "actions": [{"tool": "<name>", ...}, ...]}\n'
        f"At most {MAX_ACTIONS_PER_TURN} actions per turn. Paths are relative "
        "to the worktree root. Read before you edit. Results come back next turn.\n\n"
        f"TOOLS\n{tools}\n\n"
        f"WRITABLE (only these paths may be written): {globs}\n\n"
        f"CHECKS (run_check by name; finish runs all of them)\n{checks}\n\n"
        f"TURN {turn} of {request.max_turns}. Call finish once the checks should pass.\n\n"
        f"TASK\n{request.task}\n\n"
    )
    index = "FILES\n" + _cap("\n".join(file_index), 12_000) + "\n\n"
    shown = "".join(f"FILE {path}\n{_cap(body, 20_000)}\n\n" for path, body in context)
    budget = max_chars - len(head) - len(index) - len(shown)
    kept: list[str] = []
    for block in reversed(history):
        if budget - len(block) < 0:
            # A turn whose results exceed what is left is shown cut, never
            # dropped: its actions and outcomes are the model's only memory.
            if budget > _MIN_TURN_CHARS:
                kept.append(_cap(block, budget))
            kept.append("[earlier turns cut to fit]\n")
            break
        kept.append(block)
        budget -= len(block)
    past = "".join(reversed(kept))
    return head + index + shown + (f"HISTORY\n{past}" if past else "")


__all__ = [
    "ALLOWED_ARGUMENTS",
    "INTEGER_ARGUMENTS",
    "REQUIRED_ARGUMENTS",
    "RESPONSE_CONTRACT",
    "TOOL_SCHEMAS",
    "build_turn_prompt",
    "parse_turn_reply",
]
