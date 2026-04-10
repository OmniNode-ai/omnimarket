# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerSessionPostMortem — Session post-mortem collector.

Collects planned vs. completed phases, stalled agents, friction events,
PR status, and carry-forward items. Produces ModelPostMortemReport, writes
a Markdown report to docs/post-mortems/, and returns a structured result.

This handler is pure — all I/O goes through injected adapters. In dry_run
mode, no filesystem writes occur.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_session_post_mortem.handlers.adapter_pr_collector import (
    collect_pr_status,
)
from omnimarket.nodes.node_session_post_mortem.models.model_post_mortem_report import (
    EnumPostMortemOutcome,
    ModelFrictionEvent,
    ModelPostMortemReport,
)

logger = logging.getLogger(__name__)

# Friction type for stalled agents
_STALL_FRICTION_TYPE = "agent_stall"

# Type alias for injectable friction reader
FrictionReader = Callable[[str], list[ModelFrictionEvent]]


class ModelPostMortemCommand(BaseModel):
    """Input command for the session post-mortem handler."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    session_label: str
    phases_planned: list[str]
    phases_completed: list[str]
    phases_failed: list[str] = Field(default_factory=list)
    phases_skipped: list[str] = Field(default_factory=list)
    carry_forward_items: list[str] = Field(default_factory=list)
    friction_dir: str = ".onex_state/friction"
    report_dir: str = "docs/post-mortems"
    dry_run: bool = False


class ModelPostMortemHandlerResult(BaseModel):
    """Result produced by HandlerSessionPostMortem."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    outcome: EnumPostMortemOutcome
    report_path: str
    report: ModelPostMortemReport
    dry_run: bool = False


class HandlerSessionPostMortem:
    """Session post-mortem collector.

    Pure handler — accepts friction reader as injectable dependency for
    testability. No direct filesystem access in the handler itself.

    Args:
        friction_reader: Callable that reads friction events from a directory.
                         Defaults to the real adapter_friction_reader.
    """

    def __init__(self, friction_reader: FrictionReader | None = None) -> None:
        if friction_reader is None:
            from omnimarket.nodes.node_session_post_mortem.handlers.adapter_friction_reader import (
                read_friction_events,
            )

            self._friction_reader: FrictionReader = read_friction_events
        else:
            self._friction_reader = friction_reader

    def handle(self, command: ModelPostMortemCommand) -> ModelPostMortemHandlerResult:
        """Execute session post-mortem collection.

        Args:
            command: Post-mortem command with phase outcomes and paths.

        Returns:
            ModelPostMortemHandlerResult with outcome, report, and report_path.
        """
        started_at = datetime.now(tz=UTC)

        # Collect friction events (empty in dry_run)
        friction_events: list[ModelFrictionEvent] = []
        if not command.dry_run:
            friction_events = self._friction_reader(command.friction_dir)

        # Derive stalled agents from friction events
        stalled_agents = [
            e.agent_id or e.event_id
            for e in friction_events
            if e.friction_type == _STALL_FRICTION_TYPE
        ]

        # Derive PR status from friction events
        prs_merged, prs_open, prs_failed = collect_pr_status(friction_events)

        # Compute outcome
        outcome = self._compute_outcome(command)

        completed_at = datetime.now(tz=UTC)

        # Determine report path
        report_path = "(dry-run)"
        if not command.dry_run:
            report_path = self._build_report_path(command, started_at)

        report = ModelPostMortemReport(
            session_id=command.session_id,
            session_label=command.session_label,
            outcome=outcome,
            phases_planned=command.phases_planned,
            phases_completed=command.phases_completed,
            phases_failed=command.phases_failed,
            phases_skipped=command.phases_skipped,
            stalled_agents=stalled_agents,
            friction_events=friction_events,
            prs_merged=prs_merged,
            prs_open=prs_open,
            prs_failed=prs_failed,
            carry_forward_items=command.carry_forward_items,
            report_path=report_path,
            started_at=started_at,
            completed_at=completed_at,
        )

        # Write Markdown report (unless dry_run)
        if not command.dry_run:
            self._write_report(report, command.report_dir, report_path)

        logger.info(
            "Post-mortem complete: session_id=%s outcome=%s report_path=%s",
            command.session_id,
            outcome.value,
            report_path,
        )

        return ModelPostMortemHandlerResult(
            session_id=command.session_id,
            outcome=outcome,
            report_path=report_path,
            report=report,
            dry_run=command.dry_run,
        )

    def _compute_outcome(
        self, command: ModelPostMortemCommand
    ) -> EnumPostMortemOutcome:
        """Derive the post-mortem outcome from phase results."""
        if not command.phases_planned:
            return EnumPostMortemOutcome.COMPLETED

        completed_set = set(command.phases_completed)
        planned_set = set(command.phases_planned)

        if not command.phases_completed:
            return EnumPostMortemOutcome.FAILED

        if completed_set >= planned_set:
            return EnumPostMortemOutcome.COMPLETED

        if command.phases_failed:
            return EnumPostMortemOutcome.PARTIAL

        # Some planned phases were skipped but none failed
        return EnumPostMortemOutcome.PARTIAL

    def _build_report_path(
        self, command: ModelPostMortemCommand, started_at: datetime
    ) -> str:
        """Build the absolute report file path."""
        date_str = started_at.strftime("%Y-%m-%d")
        short_id = command.session_id[:8]
        filename = f"{date_str}-session-{short_id}.md"
        report_dir = os.path.abspath(command.report_dir)
        return os.path.join(report_dir, filename)

    def _write_report(
        self, report: ModelPostMortemReport, report_dir: str, report_path: str
    ) -> None:
        """Write Markdown post-mortem report to disk."""
        os.makedirs(os.path.abspath(report_dir), exist_ok=True)
        markdown = self._render_markdown(report)
        with open(report_path, "w", encoding="utf-8") as fh:
            fh.write(markdown)

    def _render_markdown(self, report: ModelPostMortemReport) -> str:
        """Render ModelPostMortemReport as a Markdown document."""
        lines: list[str] = [
            f"# Session Post-Mortem: {report.session_label}",
            "",
            f"**Session ID**: `{report.session_id}`  ",
            f"**Outcome**: `{report.outcome.value}`  ",
            f"**Started**: {report.started_at.isoformat()}  ",
            f"**Completed**: {report.completed_at.isoformat()}  ",
            "",
            "## Phases",
            "",
            f"- **Planned**: {', '.join(report.phases_planned) or '(none)'}",
            f"- **Completed**: {', '.join(report.phases_completed) or '(none)'}",
            f"- **Failed**: {', '.join(report.phases_failed) or '(none)'}",
            f"- **Skipped**: {', '.join(report.phases_skipped) or '(none)'}",
            "",
        ]

        if report.stalled_agents:
            lines += [
                "## Stalled Agents",
                "",
                *[f"- {a}" for a in report.stalled_agents],
                "",
            ]

        if report.friction_events:
            lines += [
                "## Friction Events",
                "",
                *[
                    f"- `{e.friction_type}`: {e.description[:120]}"
                    for e in report.friction_events
                ],
                "",
            ]

        if report.prs_merged or report.prs_open or report.prs_failed:
            lines += [
                "## PR Status",
                "",
                f"- **Merged**: {len(report.prs_merged)}",
                f"- **Open**: {len(report.prs_open)}",
                f"- **Failed**: {len(report.prs_failed)}",
                "",
            ]

        if report.carry_forward_items:
            lines += [
                "## Carry-Forward Items",
                "",
                *[f"- {item}" for item in report.carry_forward_items],
                "",
            ]

        return "\n".join(lines)


__all__: list[str] = [
    "FrictionReader",
    "HandlerSessionPostMortem",
    "ModelPostMortemCommand",
    "ModelPostMortemHandlerResult",
]
