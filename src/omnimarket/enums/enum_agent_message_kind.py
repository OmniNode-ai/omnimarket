# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The closed set of agent message kinds."""

from __future__ import annotations

from enum import StrEnum


class EnumAgentMessageKind(StrEnum):
    """What a message is: a plain message, a question that wants an answer, the answer, or a notice that wants none."""

    MESSAGE = "message"
    QUESTION = "question"
    ANSWER = "answer"
    NOTICE = "notice"
