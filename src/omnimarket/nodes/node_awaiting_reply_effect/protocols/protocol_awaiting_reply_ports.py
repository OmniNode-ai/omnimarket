# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The three transports the awaiting-reply effect sends through."""

from __future__ import annotations

from typing import Protocol


class ProtocolSlackSender(Protocol):
    """Posts into an existing Slack conversation as the operator; returns the permalink."""

    def send(self, conversation_id: str, text: str) -> str: ...


class ProtocolLinearCommenter(Protocol):
    """Adds one comment to a Linear ticket; returns the comment id or url."""

    def comment(self, ticket_id: str, body: str) -> str: ...


class ProtocolLedgerAppender(Protocol):
    """Appends one pre-formed row to the rolling ledger."""

    def append(self, row: str) -> None: ...
