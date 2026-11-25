# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The agent_messages table's columns and its one INSERT: the first row for a partition and offset is kept."""

from __future__ import annotations

from typing import Final

AGENT_MESSAGE_COLUMNS: Final[tuple[str, ...]] = (
    "source_partition",
    "source_offset",
    "status",
    "refusal_reason",
    "refusal_detail",
    "from_agent",
    "to_agent",
    "thread",
    "kind",
    "platform",
    "text",
    "sent_at",
    "broker_at",
    "sender_verified",
    "projected_at",
)

_TABLE: Final[str] = "public.agent_messages"


def insert_statement() -> str:
    """INSERT ... ON CONFLICT DO NOTHING on the primary key; RETURNING answers one row for a new message and none for a replay."""
    columns = ", ".join(AGENT_MESSAGE_COLUMNS)
    placeholders = ", ".join(f"${i}" for i in range(1, len(AGENT_MESSAGE_COLUMNS) + 1))
    return (
        f"INSERT INTO {_TABLE} ({columns}) VALUES ({placeholders}) "
        "ON CONFLICT (source_partition, source_offset) DO NOTHING RETURNING source_offset"
    )
