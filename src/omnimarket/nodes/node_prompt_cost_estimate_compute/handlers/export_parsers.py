# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read a visitor's usage export into one record per user prompt.

Four shapes are read: the ChatGPT ``conversations.json``, the Claude data
export's ``conversations.json``, Claude Code session JSONL, and a CSV with a
``prompt`` column. Claude Code usage is folded by the history reader the
savings estimate already uses, so a transcript is counted the same way here as
there. Where the export carries no token counts they are estimated from text
length and the record says so; where it names no model the record's model is
empty and the caller decides what that means.
"""

from __future__ import annotations

import csv
import io
import json
import math
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from omnimarket.nodes.node_claude_code_history_read_effect import (
    EnumHistoryReadStatus,
    HandlerClaudeCodeHistoryRead,
    ModelClaudeCodeHistoryReadRequest,
)
from omnimarket.nodes.node_prompt_cost_estimate_compute.models import EnumExportFormat

# A token count read from text length, not from the provider: about four
# characters per token for English prose and code. Used only where the export
# carries no counts, and every record it touches is marked estimated.
_CHARS_PER_TOKEN = 4


class ExportParseError(ValueError):
    """The upload is not the export shape it was declared as."""


@dataclass(frozen=True)
class ParsedPrompt:
    """One user prompt and the usage the export records for its answer."""

    conversation_id: str
    occurred_at: datetime | None
    prompt_text: str
    model: str
    tokens_in: int
    tokens_out: int
    tokens_estimated: bool


def estimate_tokens(text: str) -> int:
    """Tokens for ``text`` by length; zero only for empty text."""
    return math.ceil(len(text) / _CHARS_PER_TOKEN) if text else 0


def _string(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _epoch(value: object) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)


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
        tokens_estimated=True,
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
                pending = (_iso(message.get("created_at")), text)
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
            tokens_estimated=False,
        )
        for record in result.records
    ]


_CSV_TOKENS_IN = ("tokens_in", "input_tokens", "prompt_tokens")
_CSV_TOKENS_OUT = ("tokens_out", "output_tokens", "completion_tokens")


def _csv_int(row: dict[str, str], names: tuple[str, ...]) -> int | None:
    for name in names:
        value = (row.get(name) or "").strip()
        if value:
            try:
                count = int(value)
            except ValueError as exc:
                raise ExportParseError(
                    f"column {name} holds {value!r}, not a whole number"
                ) from exc
            if count < 0:
                raise ExportParseError(f"column {name} holds a negative count")
            return count
    return None


def _parse_csv(content: str) -> list[ParsedPrompt]:
    reader = csv.DictReader(io.StringIO(content))
    fields = {name.strip().lower() for name in reader.fieldnames or ()}
    if "prompt" not in fields:
        raise ExportParseError("the CSV has no prompt column")
    prompts: list[ParsedPrompt] = []
    for raw in reader:
        row = {(key or "").strip().lower(): (value or "") for key, value in raw.items()}
        text = row.get("prompt", "").strip()
        if not text:
            continue
        tokens_in = _csv_int(row, _CSV_TOKENS_IN)
        tokens_out = _csv_int(row, _CSV_TOKENS_OUT)
        prompts.append(
            ParsedPrompt(
                conversation_id=row.get("conversation_id", "").strip(),
                occurred_at=_iso(row.get("timestamp")),
                prompt_text=text,
                model=row.get("model", "").strip(),
                tokens_in=(
                    tokens_in if tokens_in is not None else estimate_tokens(text)
                ),
                tokens_out=(
                    tokens_out
                    if tokens_out is not None
                    else estimate_tokens(row.get("response", "").strip())
                ),
                tokens_estimated=tokens_in is None or tokens_out is None,
            )
        )
    return prompts


def parse_export(export_format: EnumExportFormat, content: str) -> list[ParsedPrompt]:
    """Read ``content`` as ``export_format``; refuse a shape that does not match."""
    if export_format is EnumExportFormat.CHATGPT_CONVERSATIONS:
        return _parse_chatgpt(content)
    if export_format is EnumExportFormat.CLAUDE_CONVERSATIONS:
        return _parse_claude(content)
    if export_format is EnumExportFormat.CLAUDE_CODE_JSONL:
        return _parse_claude_code(content)
    return _parse_csv(content)
