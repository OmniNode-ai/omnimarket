# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The compaction command's ``input_messages_hash``, derived from the hook input (OMN-21099).

The hash is the one the package reply already defines for what it installs
(``installed_messages_hash``): sha256 over the canonical JSON of the ordered
``{role, text}`` list. Here it is taken over the conversation being compacted.

Two hook inputs reach the adapter:

- the rendered ``session.compact`` module sends the engine's event, whose
  ``messages`` are the transcript being compacted (each ``{role, text, ...}``);
- a classic ``PreCompact`` command hook sends ``transcript_path``, the session's
  JSONL transcript, read here line by line: each ``user`` or ``assistant`` line
  that is not ``isMeta`` gives one message, its text the string content or the
  ``text`` blocks joined by a newline.

``messages`` wins when present. Neither present, or an unreadable transcript,
derives nothing (None), and the command then fails its own validation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from omnimarket.models.context_compaction.model_context_compaction_package_reply import (
    installed_messages_hash,
)
from omnimarket.models.context_pack_render.model_context_pack_render_message import (
    ModelContextPackRenderMessage,
)

if TYPE_CHECKING:
    from pydantic import JsonValue

_TRANSCRIPT_ROLES = frozenset({"user", "assistant"})


def _event_messages(
    messages: list[JsonValue],
) -> tuple[ModelContextPackRenderMessage, ...]:
    out: list[ModelContextPackRenderMessage] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role, text = m.get("role"), m.get("text")
        if isinstance(role, str) and isinstance(text, str):
            out.append(ModelContextPackRenderMessage(role=role, text=text))
    return tuple(out)


def _block_text(content: JsonValue) -> str | None:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    texts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                texts.append(text)
    return "\n".join(texts)


def _transcript_messages(path: Path) -> tuple[ModelContextPackRenderMessage, ...]:
    out: list[ModelContextPackRenderMessage] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if (
            not isinstance(entry, dict)
            or entry.get("type") not in _TRANSCRIPT_ROLES
            or entry.get("isMeta") is True
        ):
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        text = _block_text(message.get("content"))
        if text is None:
            continue
        role = message.get("role")
        out.append(
            ModelContextPackRenderMessage(
                role=role if isinstance(role, str) else str(entry["type"]),
                text=text,
            )
        )
    return tuple(out)


def input_messages_hash(hook_input: dict[str, JsonValue]) -> str | None:
    """sha256 of the compacted conversation's {role, text} list, or None."""
    messages = hook_input.get("messages")
    if isinstance(messages, list):
        return installed_messages_hash(_event_messages(messages))
    transcript = hook_input.get("transcript_path")
    if not isinstance(transcript, str) or not transcript:
        return None
    try:
        return installed_messages_hash(_transcript_messages(Path(transcript)))
    except (OSError, ValueError):
        return None


__all__ = ["input_messages_hash"]
