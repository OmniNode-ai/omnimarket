# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerTwoStrikeArbiter — enforces two-strike diagnosis rule.

After 2 consecutive fix failures for the same ticket:
1. Writes docs/diagnosis-<ticket>-<date>.md with error details
2. Escalates the Linear ticket to Blocked state
3. Files a friction event for the session post-mortem

All side effects are injectable for testing.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from omnimarket.nodes.node_two_strike_arbiter.models.model_two_strike_input import (
    ModelTwoStrikeCommand,
)
from omnimarket.nodes.node_two_strike_arbiter.models.model_two_strike_result import (
    EnumArbiterAction,
    ModelTwoStrikeResult,
)

_log = logging.getLogger(__name__)

_STRIKE_THRESHOLD = 2


@runtime_checkable
class DiagnosisWriter(Protocol):
    """Protocol for writing diagnosis files."""

    def write_diagnosis(
        self,
        ticket_id: str,
        content: str,
        *,
        dry_run: bool,
    ) -> str | None: ...


@runtime_checkable
class LinearUpdater(Protocol):
    """Protocol for updating Linear ticket state."""

    def move_to_blocked(self, ticket_id: str, *, dry_run: bool) -> bool: ...


@runtime_checkable
class FrictionRecorder(Protocol):
    """Protocol for filing friction events."""

    def record_friction(
        self,
        ticket_id: str,
        friction_type: str,
        description: str,
        *,
        dry_run: bool,
    ) -> bool: ...


class FileSystemDiagnosisWriter:
    """Writes diagnosis markdown files to docs/ directory."""

    def __init__(self, docs_dir: str = "docs") -> None:
        self._docs_dir = docs_dir

    def write_diagnosis(
        self,
        ticket_id: str,
        content: str,
        *,
        dry_run: bool,
    ) -> str | None:
        if dry_run:
            _log.info("dry_run: would write diagnosis for %s", ticket_id)
            return f"(dry-run) docs/diagnosis-{ticket_id}.md"
        date_str = datetime.now(tz=UTC).strftime("%Y-%m-%d")
        filename = f"diagnosis-{ticket_id}-{date_str}.md"
        dir_path = os.path.abspath(self._docs_dir)
        os.makedirs(dir_path, exist_ok=True)
        full_path = os.path.join(dir_path, filename)
        with open(full_path, "w", encoding="utf-8") as fh:
            fh.write(content)
        _log.info("Wrote diagnosis: %s", full_path)
        return full_path


def _build_diagnosis_content(
    command: ModelTwoStrikeCommand,
) -> str:
    """Build the markdown content for a diagnosis file."""
    now = datetime.now(tz=UTC).isoformat()
    lines = [
        f"# Diagnosis: {command.ticket_id}",
        "",
        f"**Generated**: {now}",
        f"**Repo**: {command.repo or '(unknown)'}",
        f"**PR**: {command.pr_number or '(none)'}",
        f"**Branch**: {command.branch or '(none)'}",
        f"**Total fix attempts**: {len(command.fix_attempts)}",
        "",
        "## Error History",
        "",
    ]
    for attempt in command.fix_attempts:
        lines.append(f"### Attempt {attempt.attempt_number} ({attempt.attempted_at})")
        lines.append("")
        lines.append(f"**Error**: {attempt.error_summary}")
        if attempt.error_detail:
            lines.append("")
            lines.append(f"```\n{attempt.error_detail}\n```")
        lines.append("")

    lines.extend(
        [
            "## Action",
            "",
            "Two consecutive failures reached. Ticket moved to Blocked.",
            "Manual investigation required before re-dispatching.",
            "",
        ]
    )
    return "\n".join(lines)


class HandlerTwoStrikeArbiter:
    """EFFECT handler that enforces the two-strike diagnosis rule.

    Dependencies are injectable via constructor for testability.
    When no side-effect adapters are injected, the handler operates in
    observation-only mode (computes the action but performs no side effects).
    """

    def __init__(
        self,
        diagnosis_writer: DiagnosisWriter | None = None,
        linear_updater: LinearUpdater | None = None,
        friction_recorder: FrictionRecorder | None = None,
    ) -> None:
        self._diagnosis_writer = diagnosis_writer
        self._linear_updater = linear_updater
        self._friction_recorder = friction_recorder

    def handle(self, command: ModelTwoStrikeCommand) -> ModelTwoStrikeResult:
        """Evaluate fix attempts and take action if threshold reached."""
        attempts = command.fix_attempts
        total = len(attempts)

        if total < _STRIKE_THRESHOLD:
            action = (
                EnumArbiterAction.FIRST_STRIKE
                if total == 1
                else EnumArbiterAction.NO_ACTION
            )
            return ModelTwoStrikeResult(
                ticket_id=command.ticket_id,
                total_attempts=total,
                action=action,
                dry_run=command.dry_run,
            )

        # Two or more strikes — take action
        diagnosis_path: str | None = None
        ticket_blocked = False
        friction_filed = False

        # Write diagnosis file
        if self._diagnosis_writer is not None:
            content = _build_diagnosis_content(command)
            diagnosis_path = self._diagnosis_writer.write_diagnosis(
                command.ticket_id,
                content,
                dry_run=command.dry_run,
            )

        # Move Linear ticket to Blocked
        if self._linear_updater is not None:
            ticket_blocked = self._linear_updater.move_to_blocked(
                command.ticket_id,
                dry_run=command.dry_run,
            )

        # File friction event
        if self._friction_recorder is not None:
            last_error = attempts[-1].error_summary if attempts else "unknown"
            friction_filed = self._friction_recorder.record_friction(
                command.ticket_id,
                friction_type="two_strike_escalation",
                description=f"Two consecutive fix failures. Last error: {last_error}",
                dry_run=command.dry_run,
            )

        # Report the strongest side effect that actually succeeded
        action = EnumArbiterAction.SECOND_STRIKE
        if friction_filed:
            action = EnumArbiterAction.FRICTION_FILED
        if ticket_blocked:
            action = EnumArbiterAction.TICKET_BLOCKED
        if diagnosis_path:
            action = EnumArbiterAction.DIAGNOSIS_WRITTEN

        _log.info(
            "Two-strike arbiter: ticket=%s attempts=%d action=%s diagnosis=%s",
            command.ticket_id,
            total,
            action.value,
            diagnosis_path,
        )

        return ModelTwoStrikeResult(
            ticket_id=command.ticket_id,
            total_attempts=total,
            action=action,
            diagnosis_path=diagnosis_path,
            friction_filed=friction_filed,
            dry_run=command.dry_run,
        )


__all__: list[str] = [
    "DiagnosisWriter",
    "FileSystemDiagnosisWriter",
    "FrictionRecorder",
    "HandlerTwoStrikeArbiter",
    "LinearUpdater",
]
