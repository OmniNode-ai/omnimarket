# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Find the operator's own messages in a Claude Code session transcript (JSON lines).

An operator message is a ``user`` entry whose content is text (not a tool result), that is not
marked ``isMeta`` (a scheduled or injected prompt) or ``isSidechain`` (a subagent), and that is
not a machine injection; or a ``queue-operation`` enqueue with text content (a message typed
while a turn was running, which never fires the prompt hook). Only the tail of the file is read,
so a long session costs the same as a short one.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from omnimarket.nodes.node_operator_capture_compute.handlers.handler_utterance_classify import (
    is_machine_injection,
)

TAIL_BYTES = 8 * 1024 * 1024


def _text_of(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if not isinstance(part, dict) or part.get("type") == "tool_result":
                return None
            if part.get("type") == "text":
                parts.append(str(part.get("text", "")))
        return "\n".join(parts) if parts else None
    return None


def _operator_text(entry: dict[str, Any]) -> str | None:
    kind = entry.get("type")
    if kind == "user":
        if entry.get("isMeta") or entry.get("isSidechain"):
            return None
        message = entry.get("message")
        if not isinstance(message, dict) or message.get("role") not in {None, "user"}:
            return None
        text = _text_of(message.get("content"))
    elif kind == "queue-operation" and entry.get("operation") == "enqueue":
        text = _text_of(entry.get("content"))
    else:
        return None
    if text is None or not text.strip() or is_machine_injection(text):
        return None
    return text


def operator_messages(
    path: Path, *, tail_bytes: int | None = TAIL_BYTES
) -> Iterator[tuple[str, datetime | None]]:
    """Each operator message in file order, with its transcript timestamp when it has one."""
    with path.open("rb") as handle:
        if tail_bytes is not None:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(size - tail_bytes, 0))
            if size > tail_bytes:
                handle.readline()
        for raw in handle:
            try:
                entry: object = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            text = _operator_text(entry)
            if text is None:
                continue
            stamp = entry.get("timestamp")
            when: datetime | None = None
            if isinstance(stamp, str):
                try:
                    when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                except ValueError:
                    when = None
            yield text, when


def latest_operator_message(path: Path) -> str | None:
    latest: str | None = None
    for text, _when in operator_messages(path):
        latest = text
    return latest


__all__ = ["TAIL_BYTES", "latest_operator_message", "operator_messages"]
