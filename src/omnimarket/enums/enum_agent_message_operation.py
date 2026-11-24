# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two operations of the agent message node."""

from __future__ import annotations

from enum import StrEnum


class EnumAgentMessageOperation(StrEnum):
    """Send one message, or read the messages addressed to one recipient."""

    SEND = "send"
    READ = "read"
