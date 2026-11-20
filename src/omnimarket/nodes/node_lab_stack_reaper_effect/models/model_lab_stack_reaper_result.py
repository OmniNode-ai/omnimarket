# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Decisions and durable evidence produced by the reaper."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProjectDecision:
    project: str
    decision: str
    open_lanes: tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ModelLabStackReaperResult:
    verb: str
    decision: str
    projects: tuple[ProjectDecision, ...] = ()
    alarms: tuple[str, ...] = ()
    ledger_rows: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return (
            self.decision not in {"refused", "aborted"}
            and not self.alarms
            and not self.errors
        )

    def line(self) -> str:
        lines = [f"lab-stack-reaper: {self.verb}: {self.decision}"]
        lines.extend(
            f"{p.project}: {p.decision}"
            + (f" (open: {', '.join(p.open_lanes)})" if p.open_lanes else "")
            + (f" (reason: {p.reason})" if p.reason else "")
            for p in self.projects
        )
        lines.extend(f"ALARM: {alarm}" for alarm in self.alarms)
        lines.extend(f"error: {error}" for error in self.errors)
        return "\n".join(lines)
