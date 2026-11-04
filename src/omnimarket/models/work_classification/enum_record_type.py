# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Known transcript record types."""

from __future__ import annotations

from enum import StrEnum


class EnumRecordType(StrEnum):
    """Values of the transcript record's type field."""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    SUMMARY = "summary"
    PR_LINK = "pr-link"
    COST_STATE = "cost-state"
    ATTACHMENT = "attachment"
    FILE_HISTORY_SNAPSHOT = "file-history-snapshot"
    QUEUE_OPERATION = "queue-operation"
    PROGRESS = "progress"
    LAST_PROMPT = "last-prompt"
    CUSTOM_TITLE = "custom-title"
    AGENT_NAME = "agent-name"
    AI_TITLE = "ai-title"
    PERMISSION_MODE = "permission-mode"
    COMPACT_BOUNDARY = "compact_boundary"
