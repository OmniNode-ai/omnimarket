# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Local definition of ModelPostMortemReport and ModelFrictionEvent.

Mirrors the wire types in omnibase_compat.telemetry.model_post_mortem_report.
When omnibase_compat ships the telemetry namespace, this file can be removed
and imports updated to point at the canonical location.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class EnumPostMortemOutcome(StrEnum):
    """Terminal outcome for a post-mortem session report."""

    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    ABORTED = "aborted"


class ModelFrictionEvent(BaseModel, frozen=True, extra="forbid"):
    """A single friction event recorded during an autonomous session.

    Parsed from files in .onex_state/friction/ by adapter_friction_reader.
    """

    event_id: str
    ticket_id: str | None = None
    agent_id: str | None = None
    friction_type: str
    description: str
    recorded_at: datetime
    schema_version: str = "1.0"


class ModelPostMortemReport(BaseModel, frozen=True, extra="forbid"):
    """Session post-mortem report produced by node_session_post_mortem.

    Cross-repo observable artifact written to docs/post-mortems/ and emitted
    to Kafka. Captures planned vs completed phases, stalled agents, friction
    events, PR status, and carry-forward items.
    """

    session_id: str
    session_label: str
    outcome: EnumPostMortemOutcome
    phases_planned: list[str]
    phases_completed: list[str]
    phases_failed: list[str]
    phases_skipped: list[str]
    stalled_agents: list[str] = Field(default_factory=list)
    friction_events: list[ModelFrictionEvent] = Field(default_factory=list)
    prs_merged: list[str] = Field(default_factory=list)
    prs_open: list[str] = Field(default_factory=list)
    prs_failed: list[str] = Field(default_factory=list)
    carry_forward_items: list[str] = Field(default_factory=list)
    report_path: str
    started_at: datetime
    completed_at: datetime
    schema_version: str = "1.0"


__all__: list[str] = [
    "EnumPostMortemOutcome",
    "ModelFrictionEvent",
    "ModelPostMortemReport",
]
