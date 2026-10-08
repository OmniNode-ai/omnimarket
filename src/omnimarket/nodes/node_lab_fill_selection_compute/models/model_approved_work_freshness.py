# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Immutable inputs and verdicts for approved-work freshness (OMN-17427)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelApprovedWorkEvidence:
    """A merged PR and its full merge commit identity."""

    pr: str
    commit: str


@dataclass(frozen=True, slots=True)
class ModelApprovedWorkTicketFacts:
    """Externally read ticket state and acceptance evidence."""

    ticket: str
    linear_state_name: str = ""
    linear_state_type: str = ""
    acceptance_status: str = ""
    evidence: tuple[ModelApprovedWorkEvidence, ...] = ()
    remaining: str = ""


@dataclass(frozen=True, slots=True)
class ModelApprovedWorkFreshnessRequest:
    """Declared rows and facts observed at a caller-supplied UTC timestamp."""

    rows: tuple[Mapping[str, object], ...]
    facts: tuple[ModelApprovedWorkTicketFacts, ...]
    checked_at: str


@dataclass(frozen=True, slots=True)
class ModelApprovedWorkVerdict:
    """The completion decision and whether it changed one declared row."""

    row_id: str
    ticket: str
    done: bool | None
    basis: str
    evidence: str
    remaining: str
    changed: bool


@dataclass(frozen=True, slots=True)
class ModelApprovedWorkFreshnessResult:
    """Fresh row copies and verdicts in the original row order."""

    rows: tuple[dict[str, object], ...]
    verdicts: tuple[ModelApprovedWorkVerdict, ...]
