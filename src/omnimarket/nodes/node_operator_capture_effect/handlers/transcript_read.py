# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Find the operator's own messages in a Claude Code session transcript (JSON lines).

An operator message is a ``user`` entry whose content is text (not a tool result), that is not
marked ``isMeta`` or ``isSidechain`` (a subagent), whose origin is human (``origin.kind`` or
``turnOrigin`` is ``human``; a transcript too old to carry either counts as human), and that is
not a machine injection. Scheduled prompts (``turnOrigin`` ``scheduled``), task notifications and
wake-ups carry another origin and are never the operator. A message typed while a turn was
running is written as such an entry when the turn takes it, so the guard sees it even though it
never fired the prompt hook. ``queue-operation`` entries are not read: a scheduler queues there
too. Only the tail of the file is read, so a long session costs the same as a short one.
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


def _is_human(entry: dict[str, Any]) -> bool:
    origin = entry.get("origin")
    turn = entry.get("turnOrigin")
    if isinstance(origin, dict) and origin.get("kind") is not None:
        return bool(origin.get("kind") == "human")
    if turn is not None:
        return bool(turn == "human")
    return True


def _operator_text(entry: dict[str, Any]) -> str | None:
    if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isSidechain"):
        return None
    if not _is_human(entry):
        return None
    message = entry.get("message")
    if not isinstance(message, dict) or message.get("role") not in {None, "user"}:
        return None
    text = _text_of(message.get("content"))
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
