# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerSessionPostMortem — Session post-mortem collector.

Collects planned vs completed phases, stalled agents, friction events,
PR status, and carry-forward items. Produces ModelPostMortemReport.
Writes to docs/post-mortems/ and emits session-post-mortem event.

The handler is pure — filesystem I/O is delegated to injectable adapters.
"""

from __future__ import annotations

import logging
from enum import StrEnum

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class EnumPostMortemOutcome(StrEnum):
    """Post-mortem session outcome."""

    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    ABORTED = "aborted"


class ModelPostMortemCommand(BaseModel, extra="forbid"):
    """Input command for HandlerSessionPostMortem."""

    session_id: str
    session_label: str
    phases_planned: list[str]
    phases_completed: list[str]
    phases_failed: list[str]
    phases_skipped: list[str] = []
    friction_dir: str = ".onex_state/friction"
    report_dir: str = "docs/post-mortems"
    dry_run: bool = False


class ModelPostMortemResult(BaseModel, extra="forbid"):
    """Output result from HandlerSessionPostMortem."""

    session_id: str
    outcome: EnumPostMortemOutcome
    report_path: str
    phases_completed: list[str]
    stalled_agents: list[str]
    friction_events_count: int


class HandlerSessionPostMortem:
    """Post-mortem handler — skeleton stub (full implementation in Task 8)."""

    def handle(self, command: ModelPostMortemCommand) -> ModelPostMortemResult:
        """Collect and write session post-mortem."""
        # Determine outcome
        if command.phases_completed and not command.phases_failed:
            # Check all planned phases completed
            all_completed = all(
                p in command.phases_completed for p in command.phases_planned
            )
            outcome = (
                EnumPostMortemOutcome.COMPLETED
                if all_completed
                else EnumPostMortemOutcome.PARTIAL
            )
        elif command.phases_completed and command.phases_failed:
            outcome = EnumPostMortemOutcome.PARTIAL
        elif not command.phases_completed:
            outcome = EnumPostMortemOutcome.FAILED
        else:
            outcome = EnumPostMortemOutcome.PARTIAL

        # Determine report path
        if command.dry_run:
            report_path = "(dry-run)"
        else:
            from datetime import UTC, datetime

            today = datetime.now(UTC).strftime("%Y-%m-%d")
            short_id = command.session_id[:8]
            report_path = f"{command.report_dir}/{today}-session-{short_id}.md"
            # Full implementation (Task 8) will write the file here

        return ModelPostMortemResult(
            session_id=command.session_id,
            outcome=outcome,
            report_path=report_path,
            phases_completed=command.phases_completed,
            stalled_agents=[],
            friction_events_count=0,
        )
