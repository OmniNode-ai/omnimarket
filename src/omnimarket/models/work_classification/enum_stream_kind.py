# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Kinds of transcript streams."""

from __future__ import annotations

from enum import StrEnum


class EnumStreamKind(StrEnum):
    """Whether a transcript represents a session or a subagent."""

    SESSION = "session"
    SUBAGENT = "subagent"
