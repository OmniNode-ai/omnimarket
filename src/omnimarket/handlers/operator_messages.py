# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Standard-library transcript semantics shared by hooks and compute nodes."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from omnimarket.enums.enum_machine_prompt import MACHINE_PREFIXES


def message_text(record: Mapping[str, Any]) -> str:
    message = record.get("message")
    content = (
        message.get("content")
        if isinstance(message, Mapping)
        else record.get("content")
    )
    if isinstance(content, str):
        return content
    return (
        "\n".join(
            value
            for block in content
            if isinstance(block, Mapping) and block.get("type") == "text"
            for value in [block.get("text")]
            if isinstance(value, str)
        )
        if isinstance(content, (list, tuple))
        else ""
    )


def is_operator_record(record: Mapping[str, Any]) -> bool:
    if (
        record.get("type") != "user"
        or record.get("isSidechain")
        or record.get("isMeta")
    ):
        return False
    message = record.get("message")
    content = (
        message.get("content")
        if isinstance(message, Mapping)
        else record.get("content")
    )
    if isinstance(content, (list, tuple)) and any(
        isinstance(b, Mapping) and b.get("type") == "tool_result" for b in content
    ):
        return False
    head = message_text(record).strip().lower()
    return bool(head) and not any(
        head.startswith(prefix) for prefix in MACHINE_PREFIXES
    )


def content_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def operator_message_id(session_id: str, native_id: str) -> str:
    return (
        "msg-" + uuid5(NAMESPACE_URL, f"operator-message:{session_id}:{native_id}").hex
    )


def record_identity(record: Mapping[str, Any], index: int) -> str:
    return str(record.get("uuid") or record.get("message_id") or f"record-{index}")


def structural_messages(
    records: Sequence[Mapping[str, Any]],
) -> tuple[list[tuple[int, Mapping[str, Any]]], list[tuple[int, Mapping[str, Any]]]]:
    """All earlier operator messages are covered by the next main-thread assistant text."""
    pending: list[tuple[int, Mapping[str, Any]]] = []
    covered: list[tuple[int, Mapping[str, Any]]] = []
    for index, record in enumerate(records):
        if record.get("isSidechain"):
            continue
        if is_operator_record(record):
            pending.append((index, record))
        elif record.get("type") == "assistant" and message_text(record).strip():
            covered.extend(pending)
            pending.clear()
    return pending, covered


def transcript_anchor(records: Sequence[Mapping[str, Any]]) -> str:
    """The previous main-thread conversation record, ignoring progress and metadata."""
    for index in range(len(records) - 1, -1, -1):
        record = records[index]
        if record.get("type") in {"user", "assistant"} and not record.get(
            "isSidechain"
        ):
            return record_identity(record, index)
    return "start"


def record_native_candidates(
    records: Sequence[Mapping[str, Any]], index: int
) -> tuple[str, ...]:
    record = records[index]
    native = record_identity(record, index)
    parent = record.get("parentUuid") or transcript_anchor(records[:index])
    return native, f"{parent}:{content_digest(message_text(record))}"
