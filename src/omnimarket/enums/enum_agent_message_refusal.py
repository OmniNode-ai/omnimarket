# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The typed refusals of the agent message node."""

from __future__ import annotations

from enum import StrEnum


class EnumAgentMessageRefusal(StrEnum):
    """Why the node did not send or read; nothing was published for any of these."""

    INVALID_MESSAGE = "invalid_message"
    BROKER_NOT_DECLARED = "broker_not_declared"
    PUBLISH_FAILED = "publish_failed"
    READ_FAILED = "read_failed"
