# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Immutable request and result of the per-lane receipt table (OMN-17427)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .enum_lane_state import EnumLaneState
from .enum_refusal_class import EnumRefusalClass


@dataclass(frozen=True, slots=True)
class ModelLaneReceiptInput:
    """One dispatched lane and its own receipt, parsed from the runner's JSON file.

    receipt is None when the file was never read. detached is False for a lane the dispatch
    never started (skipped at dispatch, or the runner did not detach); skipped and detail are
    the dispatch's own words for it."""

    lane: str
    ticket: str = ""
    model: str = ""
    planned_host: str = ""
    pinned: bool = False
    detached: bool = True
    skipped: str = ""
    detail: str = ""
    receipt: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ModelAdmittingHost:
    """A host the capacity read says admits lanes now: its free lanes and the engines it can run."""

    name: str
    free_lanes: int
    engines: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ModelLaneReceiptTableRequest:
    """The lanes of one run, and the hosts that admit a capacity retry."""

    lanes: tuple[ModelLaneReceiptInput, ...]
    admitting_hosts: tuple[ModelAdmittingHost, ...] = ()


@dataclass(frozen=True, slots=True)
class ModelLaneRow:
    """One row of the table: the reason is the receipt's own text, quoted verbatim."""

    lane: str
    ticket: str
    model: str
    host: str
    state: EnumLaneState
    refusal_class: EnumRefusalClass
    reason: str


@dataclass(frozen=True, slots=True)
class ModelLaneRetry:
    """One capacity refusal and the other admitting host it is sent to, once."""

    lane: str
    model: str
    from_host: str
    to_host: str


@dataclass(frozen=True, slots=True)
class ModelLaneReceiptTableResult:
    """The rows, the rendered markdown table, the count of each state and the planned retries."""

    rows: tuple[ModelLaneRow, ...]
    table: str
    counts: tuple[tuple[str, int], ...] = field(default_factory=tuple)
    retries: tuple[ModelLaneRetry, ...] = ()
