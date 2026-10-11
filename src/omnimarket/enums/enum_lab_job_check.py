# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Enums of the lab job check sweep: its scope and the deadlines it reports."""

from __future__ import annotations

from enum import StrEnum


class EnumLabJobCheckScope(StrEnum):
    """Which jobs a sweep visits. ``nonterminal`` is every job outside done and alerted."""

    NONTERMINAL = "nonterminal"


class EnumLabJobDeadline(StrEnum):
    """A deadline of the state table that has elapsed for a job.

    ``dispatch`` is 30 min queued, ``claim`` 10 min dispatched with no CLAIM,
    ``stop`` 10 min stopping, ``alert`` 15 min alerting with no ledger receipt,
    and ``backoff`` is the job's own ``next_dispatch_at``.
    """

    DISPATCH = "dispatch"
    CLAIM = "claim"
    STOP = "stop"
    ALERT = "alert"
    BACKOFF = "backoff"


__all__: list[str] = ["EnumLabJobCheckScope", "EnumLabJobDeadline"]
