# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read opted-in Claude Code history into prompt counts and usage."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

from omnimarket.models.savings_estimate import ModelClaudeCodePromptRecord
from omnimarket.nodes.node_claude_code_history_read_effect.models.model_history_read import (
    EnumHistoryReadStatus,
    ModelClaudeCodeHistoryReadRequest,
    ModelClaudeCodeHistoryReadResult,
)


class ProtocolClaudeCodeHistorySource(Protocol):
    """Source of session transcripts, opened only after opt-in."""

    def exists(self) -> bool: ...

    def iter_sessions(self) -> Iterator[tuple[str, str, Iterable[str]]]: ...


class FilesystemClaudeCodeHistorySource:
    """Read session files in deterministic path order."""

    def __init__(self, *, root: Path, project_filter: str | None = None) -> None:
        self._root = root
        self._project_filter = project_filter

    def exists(self) -> bool:
        return self._root.is_dir()

    def iter_sessions(self) -> Iterator[tuple[str, str, Iterable[str]]]:
        for path in sorted(self._root.rglob("*.jsonl")):
            if not path.is_file():
                continue
            if self._project_filter is not None and self._project_filter not in str(
                path
            ):
                continue
            project = path.relative_to(self._root).parts[0]
            with path.open(encoding="utf-8", errors="replace") as lines:
                yield project, path.stem, lines


def _filesystem_source(
    request: ModelClaudeCodeHistoryReadRequest,
) -> ProtocolClaudeCodeHistorySource:
    return FilesystemClaudeCodeHistorySource(
        root=Path(request.source_root).expanduser(),
        project_filter=request.project_filter,
    )


def _nonempty_string(value: object) -> str:
    return value if isinstance(value, str) and value.strip() else ""


def _prompt_text(content: object) -> str:
    if isinstance(content, str):
        return _nonempty_string(content)
    if not isinstance(content, list):
        return ""
    text = "\n".join(
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    )
    return _nonempty_string(text)


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def _tokens(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _fold_session(
    project: str, stem: str, lines: Iterable[str]
) -> tuple[list[ModelClaudeCodePromptRecord], int]:
    """Fold distinct assistant messages into the most recent user prompt."""
    entries: list[dict[str, object]] = []
    skipped = 0
    session_id = ""
    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if not isinstance(entry, dict):
            skipped += 1
            continue
        entries.append(cast("dict[str, object]", entry))
        if not session_id:
            session_id = _nonempty_string(entry.get("sessionId"))
    session_id = session_id or stem

    records: list[ModelClaudeCodePromptRecord] = []
    current: ModelClaudeCodePromptRecord | None = None
    seen: set[str] = set()
    prompt_index = 0
    for entry in entries:
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        if entry.get("type") == "user" and not entry.get("isMeta"):
            text = _prompt_text(message.get("content"))
            if not text:
                continue
            prompt_id = _nonempty_string(entry.get("uuid")) or (
                f"{session_id}:{prompt_index}"
            )
            prompt_index += 1
            if current is not None:
                records.append(current)
            current = None
            occurred_at = _timestamp(entry.get("timestamp"))
            if occurred_at is None:
                skipped += 1
                continue
            current = ModelClaudeCodePromptRecord(
                session_id=session_id,
                prompt_id=prompt_id,
                project=project,
                occurred_at=occurred_at,
                prompt_chars=len(text),
            )
        elif entry.get("type") == "assistant":
            if current is None or message.get("model") == "<synthetic>":
                continue
            message_id = (
                _nonempty_string(message.get("id"))
                or _nonempty_string(entry.get("requestId"))
                or _nonempty_string(entry.get("uuid"))
            )
            if message_id:
                if message_id in seen:
                    continue
                seen.add(message_id)
            usage = message.get("usage")
            if not isinstance(usage, dict):
                usage = {}
            current = current.model_copy(
                update={
                    "model": current.model or _nonempty_string(message.get("model")),
                    "assistant_messages": current.assistant_messages + 1,
                    "tokens_in": current.tokens_in + _tokens(usage.get("input_tokens")),
                    "tokens_out": current.tokens_out
                    + _tokens(usage.get("output_tokens")),
                    "cache_creation_tokens": current.cache_creation_tokens
                    + _tokens(usage.get("cache_creation_input_tokens")),
                    "cache_read_tokens": current.cache_read_tokens
                    + _tokens(usage.get("cache_read_input_tokens")),
                }
            )
    if current is not None:
        records.append(current)
    return records, skipped


class HandlerClaudeCodeHistoryRead:
    """Enforce opt-in before opening a source and project its usage records."""

    def __init__(
        self,
        source_factory: Callable[
            [ModelClaudeCodeHistoryReadRequest], ProtocolClaudeCodeHistorySource
        ]
        | None = None,
    ) -> None:
        self._source_factory = (
            _filesystem_source if source_factory is None else source_factory
        )

    def handle(
        self, request: ModelClaudeCodeHistoryReadRequest
    ) -> ModelClaudeCodeHistoryReadResult:
        if request.opt_in is not True:
            return ModelClaudeCodeHistoryReadResult(
                status=EnumHistoryReadStatus.REFUSED_NO_OPT_IN
            )
        source = self._source_factory(request)
        if not source.exists():
            return ModelClaudeCodeHistoryReadResult(
                status=EnumHistoryReadStatus.SOURCE_MISSING
            )

        records: list[ModelClaudeCodePromptRecord] = []
        files_read = 0
        lines_skipped = 0
        for project, stem, lines in source.iter_sessions():
            files_read += 1
            session_records, skipped = _fold_session(project, stem, lines)
            lines_skipped += skipped
            records.extend(
                record
                for record in session_records
                if (request.since is None or record.occurred_at >= request.since)
                and (request.until is None or record.occurred_at < request.until)
            )
        records.sort(
            key=lambda record: (record.occurred_at, record.session_id, record.prompt_id)
        )
        return ModelClaudeCodeHistoryReadResult(
            status=EnumHistoryReadStatus.READ,
            records=tuple(records),
            files_read=files_read,
            lines_skipped=lines_skipped,
        )
