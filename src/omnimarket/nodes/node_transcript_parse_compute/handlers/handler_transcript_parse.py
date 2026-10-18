# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerTranscriptParse: Claude Code JSONL text -> typed turns (OMN-21129, plan task TC.6).

Pure and definition-B: ``handle(ModelTranscriptParseRequest) -> ModelTranscriptParseResult``,
no event envelope, no I/O, no clock. Ported from the archive session
extractor ``extract_session_archive.py`` (``extract_user_text`` and the per-line loop of
``process_file``); its SQLite writes and its default transcript directory are not ported.
A caller reads the text first (``node_transcript_read_effect`` in pinned mode).

Text is read exactly as ``extract_user_text`` read it, so a prompt's text and the old
script's output for the same record are equal. What the old script did not do is tell an
operator-typed prompt from a synthetic one; the P8a transcript-form rule below does,
from the fields the 2.1.288 and 2.1.296 capability probes observed.
"""

from __future__ import annotations

import json
from typing import Any

from omnimarket.models.transcript_parse import (
    EnumTranscriptPromptClass,
    EnumTranscriptRecordRole,
    ModelTranscriptParseRequest,
    ModelTranscriptParseResult,
    ModelTranscriptTurn,
)

#: Versioned with the rule in ``classify_user_record``; a change to the rule bumps it.
P8A_RULE_VERSION = "p8a-transcript/1"

_ROLES = {role.value: role for role in EnumTranscriptRecordRole}


def extract_user_text(message: dict[str, Any]) -> str | None:
    """The old extractor's text rule, unchanged: string content, or text blocks joined by newline."""
    content = message.get("content", "")
    if isinstance(content, str):
        return content if content.strip() else None
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            ):
                parts.append(block["text"])
        return "\n".join(parts) if parts else None
    return None


def _has_tool_result(message: dict[str, Any]) -> bool:
    content = message.get("content")
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "tool_result"
        for block in content
    )


def classify_user_record(
    record: dict[str, Any], text: str | None
) -> EnumTranscriptPromptClass:
    """P8a, transcript form: an operator-typed prompt carries ``promptSource``.

    Native compaction summaries (``isCompactSummary``), meta records (``isMeta``),
    tool results, installed replacement records and slash-command records (``promptId``
    without ``promptSource``) are synthetic. The order of the checks fixes which reason a
    record carrying several of these marks is given.
    """
    if not text:
        return EnumTranscriptPromptClass.NOT_A_PROMPT
    message = record.get("message")
    if record.get("isCompactSummary") is True:
        return EnumTranscriptPromptClass.SYNTHETIC_COMPACT_SUMMARY
    if record.get("isMeta") is True:
        return EnumTranscriptPromptClass.SYNTHETIC_META
    if isinstance(message, dict) and _has_tool_result(message):
        return EnumTranscriptPromptClass.SYNTHETIC_TOOL_RESULT
    source = record.get("promptSource")
    if not isinstance(source, str) or not source:
        return EnumTranscriptPromptClass.SYNTHETIC_NO_PROMPT_SOURCE
    return EnumTranscriptPromptClass.OPERATOR_TYPED


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _text(record: dict[str, Any], role: EnumTranscriptRecordRole) -> str | None:
    if role is EnumTranscriptRecordRole.SYSTEM:
        content = record.get("content")
        return content if isinstance(content, str) and content.strip() else None
    message = record.get("message")
    return extract_user_text(message) if isinstance(message, dict) else None


class HandlerTranscriptParse:
    """Parse one transcript's JSONL text into user, assistant and system turns."""

    def handle(
        self, request: ModelTranscriptParseRequest
    ) -> ModelTranscriptParseResult:
        text = request.transcript_text
        turns: list[ModelTranscriptTurn] = []
        undecodable: list[int] = []
        skipped = 0
        start = 0
        line_index = 0
        while start < len(text):
            newline = text.find("\n", start)
            end = len(text) if newline == -1 else newline
            line = text[start:end]
            if line.strip():
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    record = None
                if not isinstance(record, dict):
                    undecodable.append(line_index)
                else:
                    role = _ROLES.get(str(record.get("type")))
                    if role is None:
                        skipped += 1
                    else:
                        body = _text(record, role)
                        prompt_class = (
                            classify_user_record(record, body)
                            if role is EnumTranscriptRecordRole.USER
                            else EnumTranscriptPromptClass.NOT_A_PROMPT
                        )
                        turns.append(
                            ModelTranscriptTurn(
                                line_index=line_index,
                                record_start=start,
                                record_end=end,
                                role=role,
                                record_uuid=_optional_str(record.get("uuid")),
                                parent_uuid=_optional_str(record.get("parentUuid")),
                                session_id=_optional_str(record.get("sessionId")),
                                prompt_id=_optional_str(record.get("promptId")),
                                timestamp=_optional_str(record.get("timestamp")),
                                text=body,
                                prompt_class=prompt_class,
                                operator_typed=prompt_class
                                is EnumTranscriptPromptClass.OPERATOR_TYPED,
                            )
                        )
            start = end + 1
            line_index += 1
        return ModelTranscriptParseResult(
            rule_version=P8A_RULE_VERSION,
            turns=tuple(turns),
            operator_typed_count=sum(1 for turn in turns if turn.operator_typed),
            undecodable_line_indexes=tuple(undecodable),
            skipped_record_count=skipped,
        )


__all__ = [
    "P8A_RULE_VERSION",
    "HandlerTranscriptParse",
    "classify_user_record",
    "extract_user_text",
]
