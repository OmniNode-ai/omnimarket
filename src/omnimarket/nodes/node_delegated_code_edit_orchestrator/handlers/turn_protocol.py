# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The turn protocol of the delegated code edit loop (OMN-20290): pure.

* ``RESPONSE_CONTRACT`` is the JSON Schema every turn's ``onex delegate`` run
  declares, so the delegation quality gate validates the reply's shape and
  nothing else (OMN-15193: a declared contract is the gate's sole authority).
  Each action is closed over its own tool's arguments, so a reply the gate
  accepts is one ``parse_turn_reply`` accepts, and a backend that declares
  structured output is constrained to it (OMN-17427).
* ``TOOL_SCHEMAS`` are the tools as a model is offered them (OpenAI function
  form), including ``replace_in_files`` for bulk edits. The tool_use rubric
  reads the same schemas to judge each call.
* ``parse_turn_reply`` turns the reply text into typed actions, or names why it
  cannot.
* ``build_turn_prompt`` renders one turn's prompt from the request, the file
  index and the history. Same inputs, same bytes.
* ``render_history`` fits the history to its budget (OMN-20291): every earlier
  turn keeps its action lines, the newest turns keep their full output, and a
  view window shown again later is not repeated.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from pydantic import ValidationError

from omnimarket.nodes.node_delegated_code_edit_orchestrator.models.model_delegated_code_edit import (
    MAX_ACTIONS_PER_TURN,
    MAX_BULK_FILES,
    MAX_READ_CHARS_PER_TURN,
    MAX_READ_ONLY_TURNS,
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
    EnumCodeEditTool.REPLACE_IN_FILES: ("old_string",),
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
    EnumCodeEditTool.REPLACE_IN_FILES: (
        "file_paths",
        "glob",
        "old_string",
        "new_string",
    ),
    EnumCodeEditTool.FORMAT: ("file_path",),
    EnumCodeEditTool.RUN_CHECK: ("name",),
    EnumCodeEditTool.FINISH: ("summary",),
}

#: Arguments that are integers or arrays; the rest are strings.
INTEGER_ARGUMENTS = frozenset({"offset"})
ARRAY_ARGUMENTS = frozenset({"file_paths"})

_DESCRIPTIONS: dict[EnumCodeEditTool, str] = {
    EnumCodeEditTool.VIEW: "Show one worktree file with line numbers, 250 lines "
    "at a time; offset (an integer) is the first line to show.",
    EnumCodeEditTool.LS: "List one worktree directory (default: the root).",
    EnumCodeEditTool.GREP: "Search worktree files for a regular expression.",
    EnumCodeEditTool.WRITE: "Create or replace one writable file with content.",
    EnumCodeEditTool.EDIT: "Replace old_string, which must occur exactly once, "
    "with new_string in one writable file.",
    EnumCodeEditTool.REPLACE_IN_FILES: "Replace EVERY occurrence of old_string "
    "with new_string in each writable file; each file is all-or-nothing. "
    "A named file without old_string is reported and left untouched. Give "
    "exactly one of file_paths or glob (same syntax as writable globs; it "
    "covers only writable files, and files without old_string are skipped); "
    f"at most {MAX_BULK_FILES} files.",
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
                    name: {"type": "array", "items": {"type": "string"}}
                    if name in ARRAY_ARGUMENTS
                    else {"type": "integer" if name in INTEGER_ARGUMENTS else "string"}
                    for name in ALLOWED_ARGUMENTS[tool]
                },
                "required": list(REQUIRED_ARGUMENTS[tool]),
            },
        },
    }
    for tool in EnumCodeEditTool
)

_ARGUMENT_SCHEMAS: dict[str, dict[str, object]] = {
    "path": {"type": "string"},
    "offset": {"type": "integer", "minimum": 1},
    "file_path": {"type": "string"},
    "file_paths": {
        "type": "array",
        "items": {"type": "string"},
        "minItems": 1,
        "maxItems": MAX_BULK_FILES,
    },
    "glob": {"type": "string"},
    "pattern": {"type": "string"},
    "content": {"type": "string"},
    "old_string": {"type": "string"},
    "new_string": {"type": "string"},
    "name": {"type": "string"},
    "summary": {"type": "string"},
}


def _action_schema(
    tool: EnumCodeEditTool,
    *,
    omit: tuple[str, ...] = (),
    require: tuple[str, ...] = (),
) -> dict[str, object]:
    required = ["tool", *REQUIRED_ARGUMENTS[tool], *require]
    properties: dict[str, object] = {"tool": {"const": tool.value}}
    for name in ALLOWED_ARGUMENTS[tool]:
        if name in omit:
            continue
        schema = _ARGUMENT_SCHEMAS[name].copy()
        if name in required and schema["type"] == "string" and name != "content":
            schema["minLength"] = 1
        properties[name] = schema
    return {
        "type": "object",
        "required": required,
        "properties": properties,
        "additionalProperties": False,
    }


_ACTION_SCHEMAS: tuple[dict[str, object], ...] = tuple(
    schema
    for tool in EnumCodeEditTool
    for schema in (
        (
            _action_schema(tool, omit=("glob",), require=("file_paths",)),
            _action_schema(tool, omit=("file_paths",), require=("glob",)),
        )
        if tool == EnumCodeEditTool.REPLACE_IN_FILES
        else (_action_schema(tool),)
    )
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
            "items": {"anyOf": list(_ACTION_SCHEMAS)},
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
        if tool == EnumCodeEditTool.REPLACE_IN_FILES:
            if ("file_paths" in raw) == ("glob" in raw):
                return (), (
                    f"action {index} (replace_in_files) needs exactly one of "
                    "file_paths or glob"
                )
            if "file_paths" in raw:
                paths = raw["file_paths"]
                if (
                    not isinstance(paths, list)
                    or not paths
                    or len(paths) > MAX_BULK_FILES
                    or any(not isinstance(path, str) or not path for path in paths)
                ):
                    return (), (
                        f"action {index} (replace_in_files) file_paths must be a "
                        "non-empty list of non-empty strings, at most "
                        f"{MAX_BULK_FILES} entries"
                    )
            else:
                glob = raw["glob"]
                if not isinstance(glob, str) or not glob:
                    return (), f"action {index} (replace_in_files) needs a string glob"
                if glob.startswith("/") or ".." in glob.split("/"):
                    return (), (
                        f"action {index} (replace_in_files) glob must be "
                        "worktree-relative without '..' segments"
                    )
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


@dataclass(frozen=True)
class HistoryAction:
    """One applied action as the history shows it."""

    #: ``> view(path='a.py') -> ok``
    header: str
    output: str
    #: (path, first line) of a view that succeeded.
    view_key: tuple[str, int] | None = None
    #: The path a write, edit or format that succeeded changed.
    changed_path: str = ""


@dataclass(frozen=True)
class HistoryTurn:
    """One earlier turn: its applied actions, or one message about it (a failed
    delegate run, an unusable reply, a refused finish)."""

    turn: int
    actions: tuple[HistoryAction, ...] = ()
    message: str = ""

    def to_json(self) -> dict[str, object]:
        """The turn as a loop receipt's resume block keeps it."""
        return {
            "turn": self.turn,
            "message": self.message,
            "actions": [
                {
                    "header": a.header,
                    "output": a.output,
                    "view_key": list(a.view_key) if a.view_key else None,
                    "changed_path": a.changed_path,
                }
                for a in self.actions
            ],
        }

    @classmethod
    def from_json(cls, value: object) -> HistoryTurn:
        """A turn from a resume block; a plain string (a receipt written before
        turns were structured) is one message."""
        if isinstance(value, str):
            return cls(0, message=value)
        row = cast("dict[str, object]", value)
        actions: list[HistoryAction] = []
        for raw in cast("list[dict[str, object]]", row.get("actions", [])):
            key = raw.get("view_key")
            pair = cast("list[object]", key) if isinstance(key, list) else None
            actions.append(
                HistoryAction(
                    header=str(raw["header"]),
                    output=str(raw["output"]),
                    view_key=(str(pair[0]), int(str(pair[1]))) if pair else None,
                    changed_path=str(raw.get("changed_path", "")),
                )
            )
        return cls(
            int(str(row["turn"])),
            actions=tuple(actions),
            message=str(row.get("message", "")),
        )


#: Characters of one action's output an elided turn still shows.
_BRIEF_OUTPUT_CHARS = 160


def _brief(output: str, view: bool) -> str:
    first = output.split("\n", 1)[0]
    whole = first == output
    if view and first.startswith("[") and not whole:
        return first + " [content elided to fit; view again only what you will edit]"
    if len(first) > _BRIEF_OUTPUT_CHARS:
        return first[:_BRIEF_OUTPUT_CHARS] + " ... [output elided to fit]"
    return first if whole else first + " [output elided to fit]"


def _render_turns(history: Sequence[HistoryTurn]) -> list[tuple[str, str]]:
    """(full, brief) renderings of each turn, oldest first.

    A view window shown again in a later turn points at that turn instead of
    repeating its lines, and a view of a file a later action changed says so,
    so an anchor is not copied from content that is no longer there.
    """
    shown_later: dict[tuple[str, int], int] = {}
    changed_later: dict[str, int] = {}
    rendered: list[tuple[str, str]] = []
    for entry in reversed(history):
        if not entry.actions:
            text = (
                entry.message if entry.message.endswith("\n") else entry.message + "\n"
            )
            rendered.append((text, _cap(text, 400)))
            continue
        full: list[str] = []
        brief: list[str] = []
        for action in reversed(entry.actions):
            body = action.output
            if action.view_key is not None:
                later = shown_later.get(action.view_key)
                if later is not None:
                    body = (
                        body.split("\n", 1)[0]
                        + f" [same window shown again in turn {later}]"
                    )
                elif action.view_key[0] in changed_later:
                    window, _, lines = body.partition("\n")
                    body = (
                        f"{window} [note: {action.view_key[0]} changed in turn "
                        f"{changed_later[action.view_key[0]]} after this view]\n"
                        + lines
                    )
                shown_later.setdefault(action.view_key, entry.turn)
            if action.changed_path:
                changed_later.setdefault(action.changed_path, entry.turn)
            full.append(f"{action.header}\n{body}\n")
            brief.append(
                f"{action.header}\n{_brief(body, action.view_key is not None)}\n"
            )
        head = f"TURN {entry.turn}\n"
        rendered.append(
            (head + "".join(reversed(full)), head + "".join(reversed(brief)))
        )
    rendered.reverse()
    return rendered


def render_history(history: Sequence[HistoryTurn], budget: int) -> str:
    """The history in at most about ``budget`` characters.

    Every turn first gets its brief form (action lines and one line of each
    output), oldest dropped only when even those do not fit; then the newest
    turns are shown in full while the budget lasts. A newest turn too large to
    show whole is shown cut, never dropped: its outcomes are the model's only
    memory of them.
    """
    turns = _render_turns(history)
    chosen = [brief for _, brief in turns]
    total = sum(len(text) for text in chosen)
    first = 0
    while first < len(chosen) and total > budget:
        total -= len(chosen[first])
        first += 1
    for index in range(len(turns) - 1, first - 1, -1):
        extra = len(turns[index][0]) - len(chosen[index])
        if total + extra <= budget:
            chosen[index] = turns[index][0]
            total += extra
            continue
        if index == len(turns) - 1:
            room = budget - (total - len(chosen[index]))
            if room > _MIN_TURN_CHARS:
                chosen[index] = _cap(turns[index][0], room)
        break
    marker = "[earlier turns cut to fit]\n" if first else ""
    return marker + "".join(chosen[first:])


def build_turn_prompt(
    request: ModelDelegatedCodeEditRequest,
    file_index: Sequence[str],
    context: Sequence[tuple[str, str]],
    history: Sequence[HistoryTurn],
    turn: int,
    *,
    max_chars: int = 90_000,
    reads_paused: bool = False,
) -> str:
    """One turn's prompt. ``history`` holds the previous turns, oldest first.
    ``reads_paused`` says this turn's reads are refused."""
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
        "to the worktree root. Read before you edit. Results come back next turn.\n"
        f"One turn's reads (view, grep, ls) show at most {MAX_READ_CHARS_PER_TURN} "
        "characters together. HISTORY keeps every earlier turn, older turns "
        "without their output: a view whose content is elided there was already "
        "read. Do not re-read whole files; view the lines you will change and "
        "edit them in the same or the next turn. After "
        f"{MAX_READ_ONLY_TURNS} turns in a row that read and change no file, the "
        "next turn's reads are refused.\n\n"
        f"TOOLS\n{tools}\n\n"
        f"WRITABLE (only these paths may be written): {globs}\n\n"
        f"CHECKS (run_check by name; finish runs all of them)\n{checks}\n\n"
        f"TURN {turn} of {request.max_turns}. Call finish once the checks should pass.\n"
        + (
            f"THIS TURN: {MAX_READ_ONLY_TURNS} or more turns in a row read and changed "
            "no file, so reads are refused this turn. Write or edit now with what "
            "HISTORY shows; a view row reads 'NNNN| text', copy only the text.\n"
            if reads_paused
            else ""
        )
        + "\n"
        f"TASK\n{request.task}\n\n"
    )
    index = "FILES\n" + _cap("\n".join(file_index), 12_000) + "\n\n"
    shown = "".join(f"FILE {path}\n{_cap(body, 20_000)}\n\n" for path, body in context)
    budget = max_chars - len(head) - len(index) - len(shown)
    past = render_history(history, budget) if history else ""
    return head + index + shown + (f"HISTORY\n{past}" if past else "")


__all__ = [
    "ALLOWED_ARGUMENTS",
    "ARRAY_ARGUMENTS",
    "INTEGER_ARGUMENTS",
    "REQUIRED_ARGUMENTS",
    "RESPONSE_CONTRACT",
    "TOOL_SCHEMAS",
    "HistoryAction",
    "HistoryTurn",
    "build_turn_prompt",
    "parse_turn_reply",
    "render_history",
]
