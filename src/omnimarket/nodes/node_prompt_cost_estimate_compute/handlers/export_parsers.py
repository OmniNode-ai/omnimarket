# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read a visitor's usage export into one record per user prompt.

Four shapes are read: the ChatGPT ``conversations.json``, the Claude data
export's ``conversations.json``, Claude Code session JSONL, and a CSV whose
columns the contract's ``config.csv_import_schema`` declares (read by
``csv_export_parser``). Claude Code usage is folded by the history reader the
savings estimate already uses, so a transcript is counted the same way here as
there. Where the export carries no token counts they are estimated from text
length and the record's token method says which side; where it names no model
the record's model is empty and the caller decides what that means.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from typing import cast

from omnimarket.nodes.node_claude_code_history_read_effect import (
    EnumHistoryReadStatus,
    HandlerClaudeCodeHistoryRead,
    ModelClaudeCodeHistoryReadRequest,
)
from omnimarket.nodes.node_prompt_cost_estimate_compute.handlers.csv_export_parser import (
    parse_csv,
)
from omnimarket.nodes.node_prompt_cost_estimate_compute.handlers.export_records import (
    ExportParseError,
    ParsedPrompt,
    estimate_tokens,
    parse_iso,
)
from omnimarket.nodes.node_prompt_cost_estimate_compute.models import (
    EnumExportFormat,
    EnumTokenMethod,
    ModelCsvImportSchema,
)

__all__ = ["ExportParseError", "ParsedPrompt", "estimate_tokens", "parse_export"]


def _string(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _epoch(value: object) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _json(content: str) -> object:
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        raise ExportParseError(f"the upload is not valid JSON: {exc.msg}") from exc


def _conversation_list(content: str, export_name: str) -> list[dict[str, object]]:
    data = _json(content)
    if not isinstance(data, list):
        raise ExportParseError(f"{export_name} must be a JSON list of conversations")
    return [cast("dict[str, object]", item) for item in data if isinstance(item, dict)]


def _chatgpt_text(message: dict[str, object]) -> str:
    content = message.get("content")
    if not isinstance(content, dict):
        return ""
    parts = content.get("parts")
    if not isinstance(parts, list):
        return _string(content.get("text"))
    return "\n".join(part for part in parts if isinstance(part, str)).strip()


def _chatgpt_messages(
    conversation: dict[str, object],
) -> list[dict[str, object]]:
    mapping = conversation.get("mapping")
    if not isinstance(mapping, dict):
        return []
    messages = [
        cast("dict[str, object]", node["message"])
        for node in mapping.values()
        if isinstance(node, dict) and isinstance(node.get("message"), dict)
    ]
    messages.sort(key=lambda message: _epoch(message.get("create_time")) or _MIN)
    return messages


_MIN = datetime.min.replace(tzinfo=UTC)


def _parse_chatgpt(content: str) -> list[ParsedPrompt]:
    prompts: list[ParsedPrompt] = []
    for conversation in _conversation_list(content, "a ChatGPT export"):
        conversation_id = _string(conversation.get("id")) or _string(
            conversation.get("conversation_id")
        )
        pending: tuple[datetime | None, str] | None = None
        model = ""
        reply: list[str] = []
        for message in _chatgpt_messages(conversation):
            author = message.get("author")
            role = _string(author.get("role")) if isinstance(author, dict) else ""
            text = _chatgpt_text(message)
            if role == "user" and text:
                if pending is not None:
                    prompts.append(_text_prompt(conversation_id, pending, model, reply))
                pending = (_epoch(message.get("create_time")), text)
                model = ""
                reply = []
            elif role == "assistant" and pending is not None:
                metadata = message.get("metadata")
                if not model and isinstance(metadata, dict):
                    model = _string(metadata.get("model_slug"))
                if text:
                    reply.append(text)
        if pending is not None:
            prompts.append(_text_prompt(conversation_id, pending, model, reply))
    return prompts


def _text_prompt(
    conversation_id: str,
    pending: tuple[datetime | None, str],
    model: str,
    reply: list[str],
) -> ParsedPrompt:
    occurred_at, text = pending
    return ParsedPrompt(
        conversation_id=conversation_id,
        occurred_at=occurred_at,
        prompt_text=text,
        model=model,
        tokens_in=estimate_tokens(text),
        tokens_out=estimate_tokens("\n".join(reply)),
        token_method=EnumTokenMethod.TEXT_LENGTH_BOTH,
    )


def _claude_text(message: dict[str, object]) -> str:
    text = _string(message.get("text"))
    if text:
        return text
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    return "\n".join(
        _string(block.get("text"))
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ).strip()


def _parse_claude(content: str) -> list[ParsedPrompt]:
    prompts: list[ParsedPrompt] = []
    for conversation in _conversation_list(content, "a Claude export"):
        conversation_id = _string(conversation.get("uuid"))
        messages = conversation.get("chat_messages")
        if not isinstance(messages, list):
            continue
        pending: tuple[datetime | None, str] | None = None
        reply: list[str] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            sender = _string(message.get("sender"))
            text = _claude_text(message)
            if sender == "human" and text:
                if pending is not None:
                    prompts.append(_text_prompt(conversation_id, pending, "", reply))
                pending = (parse_iso(message.get("created_at")), text)
                reply = []
            elif sender == "assistant" and pending is not None and text:
                reply.append(text)
        if pending is not None:
            prompts.append(_text_prompt(conversation_id, pending, "", reply))
    return prompts


class _UploadedTranscript:
    """The history reader's source protocol over one uploaded transcript."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def exists(self) -> bool:
        return True

    def iter_sessions(self) -> Iterator[tuple[str, str, Iterable[str]]]:
        yield "", "upload", self._lines


def _claude_code_prompt_texts(lines: list[str]) -> dict[str, str]:
    texts: dict[str, str] = {}
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "user":
            continue
        if entry.get("isMeta"):
            continue
        message = entry.get("message")
        uuid = _string(entry.get("uuid"))
        if isinstance(message, dict) and uuid:
            texts[uuid] = _claude_text(message) or _string(message.get("content"))
    return texts


def _parse_claude_code(content: str) -> list[ParsedPrompt]:
    lines = [line for line in content.splitlines() if line.strip()]
    result = HandlerClaudeCodeHistoryRead(
        source_factory=lambda _request: _UploadedTranscript(lines)
    ).handle(ModelClaudeCodeHistoryReadRequest(opt_in=True, source_root="upload"))
    if result.status is not EnumHistoryReadStatus.READ:
        raise ExportParseError(f"the transcript could not be read: {result.status}")
    if not result.records and result.lines_skipped:
        raise ExportParseError("the upload is not Claude Code session JSONL")
    texts = _claude_code_prompt_texts(lines)
    return [
        ParsedPrompt(
            conversation_id=record.session_id,
            occurred_at=record.occurred_at,
            prompt_text=texts.get(record.prompt_id, ""),
            model=record.model,
            tokens_in=record.tokens_in,
            tokens_out=record.tokens_out,
            token_method=EnumTokenMethod.REPORTED,
        )
        for record in result.records
    ]


def parse_export(
    export_format: EnumExportFormat, content: str, csv_schema: ModelCsvImportSchema
) -> list[ParsedPrompt]:
    """Read ``content`` as ``export_format``; refuse a shape that does not match."""
    if export_format is EnumExportFormat.CHATGPT_CONVERSATIONS:
        return _parse_chatgpt(content)
    if export_format is EnumExportFormat.CLAUDE_CONVERSATIONS:
        return _parse_claude(content)
    if export_format is EnumExportFormat.CLAUDE_CODE_JSONL:
        return _parse_claude_code(content)
    return parse_csv(content, csv_schema)
