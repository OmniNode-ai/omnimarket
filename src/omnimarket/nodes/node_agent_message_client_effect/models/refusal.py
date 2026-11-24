# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed refusals of the agent message client."""

from __future__ import annotations

from enum import StrEnum


class EnumAgentMessageRefusal(StrEnum):
    MISSING_RECIPIENT = "missing_recipient"
    MISSING_THREAD = "missing_thread"
    EMPTY_TEXT = "empty_text"
    UNKNOWN_PLATFORM = "unknown_platform"
    UNKNOWN_KIND = "unknown_kind"
    BAD_SINCE = "bad_since"
    BAD_LIMIT = "bad_limit"
    UNDECLARED_BROKER = "undeclared_broker"
    BROKER_REFUSED = "broker_refused"
    NODE_UNAVAILABLE = "node_unavailable"


#: Process exit code per refusal: 2 for a request the caller must fix, 3 for a broker problem.
REFUSAL_EXIT_CODES: dict[EnumAgentMessageRefusal, int] = {
    EnumAgentMessageRefusal.MISSING_RECIPIENT: 2,
    EnumAgentMessageRefusal.MISSING_THREAD: 2,
    EnumAgentMessageRefusal.EMPTY_TEXT: 2,
    EnumAgentMessageRefusal.UNKNOWN_PLATFORM: 2,
    EnumAgentMessageRefusal.UNKNOWN_KIND: 2,
    EnumAgentMessageRefusal.BAD_SINCE: 2,
    EnumAgentMessageRefusal.BAD_LIMIT: 2,
    EnumAgentMessageRefusal.UNDECLARED_BROKER: 3,
    EnumAgentMessageRefusal.BROKER_REFUSED: 3,
    EnumAgentMessageRefusal.NODE_UNAVAILABLE: 3,
}


class AgentMessageRefusalError(Exception):
    """A request the client refuses; ``code`` is the typed reason, ``detail`` says what to change."""

    def __init__(self, code: EnumAgentMessageRefusal, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail

    @property
    def exit_code(self) -> int:
        return REFUSAL_EXIT_CODES[self.code]
