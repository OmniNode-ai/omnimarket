# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and output for the dispatch-time ticket text read."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelTicketTextRequest:
    """``endpoint`` resolves (url, key) on the launching host and raises TicketTextError when it cannot."""

    brief: str
    ticket: str | None
    endpoint: Callable[[], tuple[str, str]]
    verbatim: bool = (
        False  # code-edit consumes the authored task without prompt enrichment
    )
    lane: str = ""  # named in the log lines the read prints through ``echo``
    echo: Callable[[str], None] | None = None


@dataclass(frozen=True)
class ModelTicketTextResult:
    brief: str
    ticket: str | None
    source: str  # linear | brief | refused | no-ticket | verbatim
    read_at: str = ""
    criteria: int = 0
    chars: int = 0
    error: str = ""
    refusal: str = ""  # ticket-unreadable | ticket-text-empty: the named reason the dispatch is refused (OMN-20607)

    def receipt(self) -> dict[str, object]:
        """The runner receipt's ``ticket_text`` cell: what was read, never the key."""
        cells = {
            "ticket": self.ticket,
            "source": self.source,
            "read_at": self.read_at,
            "criteria": self.criteria,
            "chars": self.chars,
            "error": self.error,
        }
        return {
            k: v for k, v in cells.items() if v not in ("", 0, None) or k == "source"
        }

    def echoes(self, lane: str) -> list[str]:
        """The runner's log lines for a read or a refused one."""
        if self.refusal:
            return [self.refusal_line(lane)]
        if self.source == "linear":
            return [
                f"REMOTE-LANE-TICKET-TEXT lane={lane} ticket={self.ticket} criteria={self.criteria} chars={self.chars}"
            ]
        return []

    def refusal_line(self, lane: str) -> str:
        """The line a refused dispatch prints and records as its error: the reason, the ticket and why."""
        return (
            f"REMOTE-LANE-TICKET-TEXT-REFUSED lane={lane} ticket={self.ticket} reason={self.refusal}: {self.error}; "
            "a headless lab lane has no Linear key or tool, so it cannot read the ticket itself (OMN-20607)"
        )
