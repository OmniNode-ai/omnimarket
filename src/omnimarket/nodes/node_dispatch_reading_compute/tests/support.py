# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared builders of the dispatch reading tests (OMN-22963): fixtures only, no node logic."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from importlib.resources import files
from typing import Any

import yaml

from omnimarket.nodes.node_dispatch_reading_compute.models import (
    ModelDispatchReadingRequest,
    ModelDispatchReadingSettings,
    ModelDispatchRecord,
    ModelLaneFact,
    ModelTerminalResult,
)

NAME = "node_dispatch_reading_compute"
NOW = datetime(2026, 11, 9, 18, 0, tzinfo=UTC)
SCRATCH_SCRIPT = "bash .claude_scratch/drill-run-244.sh"


def overlay() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(
        files(f"omnimarket.nodes.{NAME}").joinpath("overlay.yaml").read_text()
    )
    return loaded


def settings() -> ModelDispatchReadingSettings:
    return ModelDispatchReadingSettings.model_validate(overlay())


def lane(
    name: str, *, kind: str = "fill", launched_from: str | None = "onex-remote-lane"
) -> ModelLaneFact:
    return ModelLaneFact(
        lane=name,
        kind=kind,
        started_at=NOW - timedelta(seconds=30),
        launched_from=launched_from,
    )


def record(
    lane_name: str,
    *,
    terminal_after: float | None = 600,
    deadline_seconds: float = 3600,
    dispatched_seconds_ago: float = 1800,
) -> ModelDispatchRecord:
    """A dispatch record; ``terminal_after`` is seconds after dispatch, None for no terminal result."""
    dispatched = NOW - timedelta(seconds=dispatched_seconds_ago)
    return ModelDispatchRecord(
        dispatch_id=f"dispatch-{lane_name}",
        lane=lane_name,
        dispatched_at=dispatched,
        deadline_at=dispatched + timedelta(seconds=deadline_seconds),
        terminal=None
        if terminal_after is None
        else ModelTerminalResult(
            at=dispatched + timedelta(seconds=terminal_after), outcome="done"
        ),
    )


def clean_lanes() -> tuple[ModelLaneFact, ...]:
    return (lane("fill-a"), lane("fill-b"))


def clean_records() -> tuple[ModelDispatchRecord, ...]:
    return (record("fill-a"), record("fill-b", terminal_after=None))


def request(
    lanes: tuple[ModelLaneFact, ...] | None = None,
    records: tuple[ModelDispatchRecord, ...] | None = None,
    *,
    age_seconds: float = 120,
    read_failure: Any = None,
    unread_lanes: bool = False,
    unread_records: bool = False,
) -> ModelDispatchReadingRequest:
    """The clean floor by default: two lanes, each with a record, one record still pending inside its deadline."""
    return ModelDispatchReadingRequest(
        correlation_id="dr-1",
        now=NOW,
        read_at=NOW - timedelta(seconds=age_seconds),
        settings=settings(),
        lanes=None if unread_lanes else (clean_lanes() if lanes is None else lanes),
        records=None
        if unread_records
        else (clean_records() if records is None else records),
        read_failure=read_failure,
    )
