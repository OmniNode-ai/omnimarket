# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerSessionPostMortem — Session post-mortem collector.

Collects planned vs completed phases, stalled agents, friction events,
PR status, and carry-forward items. Writes to docs/post-mortems/ and
emits session-post-mortem event.

The handler is pure — filesystem I/O is delegated to injectable adapters.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class EnumPostMortemOutcome(StrEnum):
    """Post-mortem session outcome."""

    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    ABORTED = "aborted"


class ModelFrictionEventLocal(BaseModel, frozen=True, extra="forbid"):
    """Local friction event model (mirrors omnibase_compat.telemetry.ModelFrictionEvent)."""

    event_id: str
    ticket_id: str | None = None
    agent_id: str | None = None
    friction_type: str
    description: str
    recorded_at: datetime
    schema_version: str = "1.0"


class ModelPostMortemCommand(BaseModel, extra="forbid"):
    """Input command for HandlerSessionPostMortem."""

    session_id: str
    session_label: str
    phases_planned: list[str]
    phases_completed: list[str]
    phases_failed: list[str]
    phases_skipped: list[str] = []
    carry_forward_items: list[str] = []
    friction_dir: str = ".onex_state/friction"
    report_dir: str = "docs/post-mortems"
    dry_run: bool = False


class ModelPostMortemResult(BaseModel, extra="forbid"):
    """Output result from HandlerSessionPostMortem."""

    session_id: str
    outcome: EnumPostMortemOutcome
    report_path: str
    phases_completed: list[str]
    phases_failed: list[str]
    phases_skipped: list[str]
    stalled_agents: list[str]
    friction_events: list[ModelFrictionEventLocal]
    carry_forward_items: list[str]
    dry_run: bool
    completed_at: datetime


class FrictionReaderProtocol(Protocol):
    """Protocol for friction reader adapter (injectable for testing)."""

    def read_friction_events(
        self, friction_dir: str
    ) -> list[ModelFrictionEventLocal]: ...


class HandlerSessionPostMortem:
    """Session post-mortem collector.

    Collects phase outcomes, friction events, and stalled agents.
    Writes a Markdown report to docs/post-mortems/ (unless dry_run).
    The friction_reader adapter is injectable for hermetic testing.
    """

    def __init__(self, friction_reader: FrictionReaderProtocol | None = None) -> None:
        from omnimarket.nodes.node_session_post_mortem.handlers.adapter_friction_reader import (
            AdapterFrictionReader,
        )

        self._friction_reader: FrictionReaderProtocol = (
            friction_reader if friction_reader is not None else AdapterFrictionReader()
        )

    def handle(self, command: ModelPostMortemCommand) -> ModelPostMortemResult:
        """Collect and write session post-mortem.

        Args:
            command: Post-mortem command with phase lists and paths.

        Returns:
            ModelPostMortemResult with outcome, report path, and friction events.
        """
        completed_at = datetime.now(UTC)

        # Collect friction events (dry_run returns empty)
        if command.dry_run:
            friction_events: list[ModelFrictionEventLocal] = []
        else:
            friction_events = self._friction_reader.read_friction_events(
                command.friction_dir
            )

        # Derive stalled agents from friction events
        stalled_agents = [
            e.agent_id or e.event_id
            for e in friction_events
            if e.friction_type == "agent_stall"
        ]

        # Compute outcome
        outcome = self._compute_outcome(command)

        # Determine report path and write if not dry_run
        if command.dry_run:
            report_path = "(dry-run)"
        else:
            today = completed_at.strftime("%Y-%m-%d")
            short_id = command.session_id[:8]
            abs_report_dir = os.path.abspath(command.report_dir)
            os.makedirs(abs_report_dir, exist_ok=True)
            report_path = os.path.join(abs_report_dir, f"{today}-session-{short_id}.md")
            markdown = self._render_markdown(
                command, friction_events, outcome, completed_at
            )
            with open(report_path, "w") as f:
                f.write(markdown)
            logger.info("Wrote post-mortem report to %s", report_path)

        return ModelPostMortemResult(
            session_id=command.session_id,
            outcome=outcome,
            report_path=report_path,
            phases_completed=command.phases_completed,
            phases_failed=command.phases_failed,
            phases_skipped=command.phases_skipped,
            stalled_agents=stalled_agents,
            friction_events=friction_events,
            carry_forward_items=command.carry_forward_items,
            dry_run=command.dry_run,
            completed_at=completed_at,
        )

    def _compute_outcome(
        self, command: ModelPostMortemCommand
    ) -> EnumPostMortemOutcome:
        """Derive session outcome from phase completion data."""
        if command.phases_completed and not command.phases_failed:
            all_planned_done = all(
                p in command.phases_completed for p in command.phases_planned
            )
            return (
                EnumPostMortemOutcome.COMPLETED
                if all_planned_done
                else EnumPostMortemOutcome.PARTIAL
            )
        if command.phases_completed and command.phases_failed:
            return EnumPostMortemOutcome.PARTIAL
        if not command.phases_completed:
            return EnumPostMortemOutcome.FAILED
        return EnumPostMortemOutcome.PARTIAL

    def _render_markdown(
        self,
        command: ModelPostMortemCommand,
        friction_events: list[ModelFrictionEventLocal],
        outcome: EnumPostMortemOutcome,
        completed_at: datetime,
    ) -> str:
        """Render the post-mortem report as Markdown."""
        lines: list[str] = [
            f"# Session Post-Mortem: {command.session_label}",
            "",
            f"**Session ID**: {command.session_id}",
            f"**Outcome**: {outcome.value}",
            f"**Completed At**: {completed_at.isoformat()}",
            "",
            "## Phase Summary",
            "",
            f"- **Planned**: {', '.join(command.phases_planned) or '(none)'}",
            f"- **Completed**: {', '.join(command.phases_completed) or '(none)'}",
            f"- **Failed**: {', '.join(command.phases_failed) or '(none)'}",
            f"- **Skipped**: {', '.join(command.phases_skipped) or '(none)'}",
            "",
            "## Friction Events",
            "",
        ]
        if friction_events:
            for evt in friction_events:
                lines.append(
                    f"- [{evt.friction_type}] {evt.description}"
                    + (f" (ticket: {evt.ticket_id})" if evt.ticket_id else "")
                )
        else:
            lines.append("_(none recorded)_")

        lines += [
            "",
            "## Carry-Forward Items",
            "",
        ]
        if command.carry_forward_items:
            for item in command.carry_forward_items:
                lines.append(f"- {item}")
        else:
            lines.append("_(none)_")

        lines.append("")
        return "\n".join(lines)


__all__: list[str] = [
    "EnumPostMortemOutcome",
    "FrictionReaderProtocol",
    "HandlerSessionPostMortem",
    "ModelFrictionEventLocal",
    "ModelPostMortemCommand",
    "ModelPostMortemResult",
]
