# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_transcript_parse_compute: parity with the archive extractor, the P8a rule, purity (OMN-21129, TC.6)."""

from __future__ import annotations

import builtins
import io
import json
import os
import pathlib
from typing import Any

import pytest

from omnimarket.models.transcript_parse import (
    EnumTranscriptPromptClass,
    EnumTranscriptRecordRole,
    ModelTranscriptParseRequest,
)
from omnimarket.nodes.node_transcript_parse_compute.handlers.handler_transcript_parse import (
    P8A_RULE_VERSION,
    HandlerTranscriptParse,
)

pytestmark = pytest.mark.unit


def old_extract_user_text(message: dict[str, Any]) -> str | None:
    """Oracle: the archive extractor's extract_user_text (extract_session_archive.py:101 at 01eb1b9), verbatim."""
    content = message.get("content", "")
    if isinstance(content, str):
        return content if content.strip() else None
    if isinstance(content, list):
        parts: list[str] = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(c["text"])
        return "\n".join(parts) if parts else None
    return None


SID = "013d2012-829d-46aa-955f-458c3f747ee7"
OPERATOR = {
    "type": "user",
    "uuid": "u-op-1",
    "parentUuid": None,
    "sessionId": SID,
    "promptId": "p-1",
    "promptSource": "terminal",
    "turnOrigin": "terminal",
    "timestamp": "2026-10-07T10:00:00.000Z",
    "message": {"role": "user", "content": "Do not deploy without approval."},
}
OPERATOR_BLOCKS = {
    "type": "user",
    "uuid": "u-op-2",
    "parentUuid": "a-1",
    "sessionId": SID,
    "promptId": "p-2",
    "promptSource": "sdk",
    "message": {
        "role": "user",
        "content": [
            {"type": "text", "text": "first part"},
            {"type": "image", "source": {"type": "base64", "data": "AAAA"}},
            {"type": "text", "text": "second part"},
        ],
    },
}
ASSISTANT = {
    "type": "assistant",
    "uuid": "a-1",
    "parentUuid": "u-op-1",
    "sessionId": SID,
    "message": {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "ok"},
            {"type": "tool_use", "id": "t-1", "name": "Bash", "input": {}},
        ],
    },
}
TOOL_RESULT = {
    "type": "user",
    "uuid": "u-tr",
    "parentUuid": "a-1",
    "sessionId": SID,
    "promptId": "p-1",
    "message": {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "t-1", "content": "x"}],
    },
}
TOOL_RESULT_WITH_TEXT = {
    "type": "user",
    "uuid": "u-trt",
    "promptId": "p-1",
    "promptSource": "terminal",
    "message": {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "t-2", "content": "x"},
            {"type": "text", "text": "hook note beside a result"},
        ],
    },
}
COMPACT_SUMMARY = {
    "type": "user",
    "uuid": "cab75a50",
    "parentUuid": "b-1",
    "promptId": "691239c4",
    "isCompactSummary": True,
    "message": {"role": "user", "content": "This session is being continued..."},
}
META_CAVEAT = {
    "type": "user",
    "uuid": "u-meta",
    "promptId": "13aa4872",
    "isMeta": True,
    "message": {
        "role": "user",
        "content": "<local-command-caveat>Caveat</local-command-caveat>",
    },
}
COMMAND = {
    "type": "user",
    "uuid": "u-cmd",
    "promptId": "13aa4872",
    "message": {"role": "user", "content": "<command-name>/compact</command-name>"},
}
REPLACEMENT = {
    "type": "user",
    "uuid": "1c523b67",
    "parentUuid": "783e1d6e",
    "promptId": "13aa4872",
    "userType": "external",
    "message": {"role": "user", "content": "PROBE-ACTIVATION-id-9f3c: carried"},
}
WHITESPACE_PROMPT = {
    "type": "user",
    "uuid": "u-ws",
    "promptSource": "terminal",
    "message": {"role": "user", "content": "   "},
}
BOUNDARY = {
    "type": "system",
    "subtype": "compact_boundary",
    "uuid": "b-1",
    "parentUuid": None,
    "content": "Conversation compacted",
}
TITLE = {"type": "ai-title", "aiTitle": "a title"}

RECORDS: list[dict[str, Any]] = [
    TITLE,
    OPERATOR,
    ASSISTANT,
    TOOL_RESULT,
    TOOL_RESULT_WITH_TEXT,
    BOUNDARY,
    COMPACT_SUMMARY,
    META_CAVEAT,
    COMMAND,
    REPLACEMENT,
    WHITESPACE_PROMPT,
    OPERATOR_BLOCKS,
]

#: The records the old extractor returned text for that the P8a rule calls synthetic.
SYNTHETIC_LISTED = {
    "u-trt": EnumTranscriptPromptClass.SYNTHETIC_TOOL_RESULT,
    "cab75a50": EnumTranscriptPromptClass.SYNTHETIC_COMPACT_SUMMARY,
    "u-meta": EnumTranscriptPromptClass.SYNTHETIC_META,
    "u-cmd": EnumTranscriptPromptClass.SYNTHETIC_NO_PROMPT_SOURCE,
    "1c523b67": EnumTranscriptPromptClass.SYNTHETIC_NO_PROMPT_SOURCE,
}


def _transcript(records: list[dict[str, Any]], *, junk: bool = True) -> str:
    lines = [json.dumps(r) for r in records]
    if junk:
        lines.insert(3, "{not json")
        lines.insert(5, "")
        lines.insert(7, "[1, 2]")
    return "\n".join(lines) + "\n"


def _parse(text: str) -> Any:
    return HandlerTranscriptParse().handle(
        ModelTranscriptParseRequest(transcript_text=text)
    )


def test_prompts_equal_the_old_extractor_except_the_listed_synthetic_records() -> None:
    """AC1: every user record's text equals the oracle; the only non-operator ones with text are listed."""
    text = _transcript(RECORDS)
    result = _parse(text)
    by_uuid = {t.record_uuid: t for t in result.turns}
    old_prompts: dict[str, str] = {}
    for line in text.splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "user":
            got = old_extract_user_text(obj["message"])
            assert by_uuid[obj["uuid"]].text == got
            if got:
                old_prompts[obj["uuid"]] = got
    typed = {t.record_uuid: t.text for t in result.turns if t.operator_typed}
    differences = {
        uuid: by_uuid[uuid].prompt_class for uuid in old_prompts if uuid not in typed
    }
    assert differences == SYNTHETIC_LISTED
    assert typed == {u: old_prompts[u] for u in ("u-op-1", "u-op-2")}
    assert by_uuid["u-op-2"].text == "first part\nsecond part"
    assert result.operator_typed_count == 2
    assert result.rule_version == P8A_RULE_VERSION


def test_offsets_slice_each_record_line_out_of_the_text() -> None:
    text = _transcript(RECORDS)
    result = _parse(text)
    lines = text.split("\n")
    for turn in result.turns:
        line = text[turn.record_start : turn.record_end]
        assert line == lines[turn.line_index]
        assert json.loads(line)["uuid"] == turn.record_uuid


def test_ids_and_roles_are_read_from_the_record_fields() -> None:
    result = _parse(_transcript(RECORDS))
    first = next(t for t in result.turns if t.record_uuid == "u-op-1")
    assert (first.role, first.prompt_id, first.session_id, first.parent_uuid) == (
        EnumTranscriptRecordRole.USER,
        "p-1",
        SID,
        None,
    )
    assert first.timestamp == "2026-10-07T10:00:00.000Z"
    boundary = next(t for t in result.turns if t.record_uuid == "b-1")
    assert boundary.role is EnumTranscriptRecordRole.SYSTEM
    assert boundary.text == "Conversation compacted"
    assistant = next(t for t in result.turns if t.record_uuid == "a-1")
    assert assistant.text == "ok"
    assert not assistant.operator_typed


def test_bad_lines_are_listed_and_other_record_types_counted() -> None:
    result = _parse(_transcript(RECORDS))
    assert result.undecodable_line_indexes == (3, 7)
    assert result.skipped_record_count == 1


def test_a_compact_summary_with_a_prompt_source_is_still_synthetic() -> None:
    """P8a positive control: isCompactSummary wins over every other field."""
    record = dict(COMPACT_SUMMARY, promptSource="terminal")
    (turn,) = _parse(json.dumps(record)).turns
    assert turn.prompt_class is EnumTranscriptPromptClass.SYNTHETIC_COMPACT_SUMMARY
    assert not turn.operator_typed


def test_a_record_with_prompt_id_and_no_prompt_source_is_not_an_operator_prompt() -> (
    None
):
    """Capability report CHANGE 7: keying on promptId alone would admit installed replacements."""
    (turn,) = _parse(json.dumps(REPLACEMENT)).turns
    assert turn.prompt_id == "13aa4872"
    assert not turn.operator_typed


def test_text_without_a_trailing_newline_and_crlf_free_input_parse_the_same() -> None:
    text = _transcript(RECORDS, junk=False)
    assert _parse(text) == _parse(text.rstrip("\n"))
    assert _parse("").turns == ()


def test_the_parse_performs_no_io(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC2: the compute opens no file; every open path raises inside the parse."""
    text = _transcript(RECORDS)

    def forbidden(*args: object, **kwargs: object) -> Any:
        raise AssertionError(f"I/O attempted: {args!r}")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(io, "open", forbidden)
    monkeypatch.setattr(os, "open", forbidden)
    monkeypatch.setattr(os, "listdir", forbidden)
    monkeypatch.setattr(os, "scandir", forbidden)
    monkeypatch.setattr(pathlib.Path, "open", forbidden)
    monkeypatch.setattr(pathlib.Path, "read_text", forbidden)
    monkeypatch.setattr(pathlib.Path, "read_bytes", forbidden)
    result = _parse(text)
    assert result.operator_typed_count == 2


def test_the_handler_source_names_no_path_or_clock() -> None:
    source = (
        pathlib.Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_transcript_parse_compute/handlers/handler_transcript_parse.py"
    ).read_text(encoding="utf-8")
    for needle in ("open(", "Path(", ".claude", "datetime", "time.", "environ"):
        assert needle not in source, needle
