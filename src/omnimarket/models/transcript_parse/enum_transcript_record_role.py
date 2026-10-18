# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Role of a Claude Code transcript record that the parser keeps as a turn (OMN-21129)."""

from __future__ import annotations

from enum import StrEnum


class EnumTranscriptRecordRole(StrEnum):
    """The record's own `type` field; other record types are counted, not kept."""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
