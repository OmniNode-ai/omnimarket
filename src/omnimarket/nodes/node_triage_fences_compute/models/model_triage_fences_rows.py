# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The parsed ledger rows and open claims of the triage fence derivation (OMN-20682)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class LedgerRow:
    """One parsed lifecycle row, normalized across all four live forms."""

    kind: str  # CLAIM | CLAIM_UPDATE | TERMINAL
    ts: datetime
    lane: str | None
    tickets: frozenset[str]
    related: frozenset[str]
    actor: str | None
    body: str
    source: str  # "<file>:<lineno>"
    form: str  # dash | bare | pipe
    raw: str
    # The carrier tickets a child-ticket CLAIM names in its parent_ticket cell (OMN-24248).
    parent_tickets: frozenset[str] = frozenset()


@dataclass
class ParseResult:
    rows: list[LedgerRow] = field(default_factory=list)
    unparsed: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class OpenClaim:
    """A claim with no matching TERMINAL: live, exclusive scope."""

    lane: str | None
    tickets: frozenset[str]
    related: frozenset[str]
    claim_ts: datetime
    last_update_ts: datetime
    update_count: int
    actor: str | None
    source: str
    form: str
    age_hours: float
    raw: str
    last_activity_ts: datetime | None = None
    live_basis: str = ""
