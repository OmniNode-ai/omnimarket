# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerAuditTrailCompactor — daily rollup of friction + dispatch logs.

Produces:
- Top failure modes (grouped by description pattern)
- Recurring tickets (tickets appearing in multiple failure entries)
- Stall heatmap (stall events grouped by agent/skill)

All I/O is injectable for testing.
"""

from __future__ import annotations

import logging
import os
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from omnimarket.nodes.node_audit_trail_compactor.models.model_audit_trail_input import (
    ModelAuditEntry,
    ModelCompactorCommand,
)
from omnimarket.nodes.node_audit_trail_compactor.models.model_audit_trail_result import (
    ModelCompactorResult,
    ModelFailureMode,
    ModelRecurringTicket,
    ModelStallHeatmapEntry,
)

_log = logging.getLogger(__name__)


@runtime_checkable
class AuditReader(Protocol):
    """Protocol for reading audit trail entries."""

    def read_friction_entries(self, friction_dir: str) -> list[ModelAuditEntry]: ...

    def read_dispatch_entries(self, log_path: str) -> list[ModelAuditEntry]: ...


@runtime_checkable
class RollupWriter(Protocol):
    """Protocol for writing rollup output."""

    def write_rollup(self, content: str, *, dry_run: bool) -> str | None: ...


class FileSystemAuditReader:
    """Reads audit entries from friction directory and dispatch log."""

    def read_friction_entries(self, friction_dir: str) -> list[ModelAuditEntry]:
        entries: list[ModelAuditEntry] = []
        dir_path = os.path.abspath(friction_dir)
        if not os.path.isdir(dir_path):
            return entries
        for filename in sorted(os.listdir(dir_path)):
            if not filename.endswith(".json"):
                continue
            filepath = os.path.join(dir_path, filename)
            try:
                import json

                with open(filepath, encoding="utf-8") as fh:
                    data = json.load(fh)
                entries.append(
                    ModelAuditEntry(
                        entry_type="friction",
                        ticket_id=data.get("ticket_id"),
                        agent_id=data.get("agent_id"),
                        description=data.get("description", ""),
                        recorded_at=data.get("recorded_at", ""),
                    )
                )
            except Exception as exc:
                _log.warning("Failed to read friction file %s: %s", filepath, exc)
        return entries

    def read_dispatch_entries(self, log_path: str) -> list[ModelAuditEntry]:
        entries: list[ModelAuditEntry] = []
        full_path = os.path.abspath(log_path)
        if not os.path.isfile(full_path):
            return entries
        try:
            import json

            with open(full_path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    outcome = data.get("outcome", "unknown")
                    entries.append(
                        ModelAuditEntry(
                            entry_type=f"dispatch_{outcome}",
                            ticket_id=data.get("ticket_id"),
                            agent_id=data.get("skill_id") or data.get("agent_id"),
                            description=data.get("error", "")
                            if outcome != "success"
                            else "",
                            recorded_at=data.get("timestamp", ""),
                        )
                    )
        except Exception as exc:
            _log.warning("Failed to read dispatch log %s: %s", full_path, exc)
        return entries


class FileSystemRollupWriter:
    """Writes rollup markdown to the reports directory."""

    def __init__(self, reports_dir: str = "docs/audit-rollups") -> None:
        self._reports_dir = reports_dir

    def write_rollup(self, content: str, *, dry_run: bool) -> str | None:
        if dry_run:
            _log.info("dry_run: would write audit rollup")
            return "(dry-run) docs/audit-rollups/"
        date_str = datetime.now(tz=UTC).strftime("%Y-%m-%d")
        filename = f"audit-rollup-{date_str}.md"
        dir_path = os.path.abspath(self._reports_dir)
        os.makedirs(dir_path, exist_ok=True)
        full_path = os.path.join(dir_path, filename)
        with open(full_path, "w", encoding="utf-8") as fh:
            fh.write(content)
        _log.info("Wrote audit rollup: %s", full_path)
        return full_path


class HandlerAuditTrailCompactor:
    """COMPUTE/EFFECT handler that compacts audit trail data into rollups.

    Reads friction events and dispatch logs, computes aggregated metrics,
    and optionally writes a markdown rollup report.
    """

    def __init__(
        self,
        audit_reader: AuditReader | None = None,
        rollup_writer: RollupWriter | None = None,
    ) -> None:
        self._reader = audit_reader or FileSystemAuditReader()
        self._writer = rollup_writer

    def handle(self, command: ModelCompactorCommand) -> ModelCompactorResult:
        """Read audit entries, compute rollup, optionally write report."""
        cutoff = datetime.now(tz=UTC) - timedelta(days=command.lookback_days)

        friction_entries = self._reader.read_friction_entries(command.friction_dir)
        dispatch_entries = self._reader.read_dispatch_entries(command.dispatch_log_path)

        all_entries = friction_entries + dispatch_entries
        filtered = [
            e
            for e in all_entries
            if (parsed := self._parse_recorded_at(e.recorded_at)) is not None
            and parsed >= cutoff
        ]

        failure_modes = self._compute_failure_modes(filtered)
        recurring_tickets = self._compute_recurring_tickets(filtered)
        stall_heatmap = self._compute_stall_heatmap(filtered)

        rollup_path: str | None = None
        if self._writer is not None:
            content = self._render_rollup(
                command,
                filtered,
                failure_modes,
                recurring_tickets,
                stall_heatmap,
            )
            rollup_path = self._writer.write_rollup(content, dry_run=command.dry_run)

        _log.info(
            "Audit compactor: %d entries, %d failure modes, %d recurring tickets, %d stall agents",
            len(filtered),
            len(failure_modes),
            len(recurring_tickets),
            len(stall_heatmap),
        )

        return ModelCompactorResult(
            total_entries=len(filtered),
            failure_modes=failure_modes,
            recurring_tickets=recurring_tickets,
            stall_heatmap=stall_heatmap,
            rollup_path=rollup_path,
            dry_run=command.dry_run,
        )

    def _compute_failure_modes(
        self,
        entries: list[ModelAuditEntry],
    ) -> list[ModelFailureMode]:
        failure_counter: Counter[str] = Counter()
        failure_tickets: dict[str, set[str]] = defaultdict(set)

        for entry in entries:
            if entry.entry_type == "dispatch_success":
                continue
            if not entry.description:
                continue
            key = self._normalize_failure_key(entry.description)
            failure_counter[key] += 1
            if entry.ticket_id:
                failure_tickets[key].add(entry.ticket_id)

        return [
            ModelFailureMode(
                description=key,
                count=count,
                ticket_ids=sorted(failure_tickets.get(key, set())),
            )
            for key, count in failure_counter.most_common(10)
        ]

    def _compute_recurring_tickets(
        self,
        entries: list[ModelAuditEntry],
    ) -> list[ModelRecurringTicket]:
        ticket_failures: dict[str, list[ModelAuditEntry]] = defaultdict(list)
        for entry in entries:
            if entry.entry_type == "dispatch_success":
                continue
            if entry.ticket_id:
                ticket_failures[entry.ticket_id].append(entry)

        recurring = [
            ModelRecurringTicket(
                ticket_id=tid,
                failure_count=len(failures),
                last_failure=(latest.description or latest.entry_type),
            )
            for tid, failures in ticket_failures.items()
            for latest in [
                max(
                    failures,
                    key=lambda entry: (
                        self._parse_recorded_at(entry.recorded_at)
                        or datetime.min.replace(tzinfo=UTC)
                    ),
                )
            ]
            if len(failures) >= 2
        ]
        recurring.sort(key=lambda r: r.failure_count, reverse=True)
        return recurring[:20]

    def _compute_stall_heatmap(
        self,
        entries: list[ModelAuditEntry],
    ) -> list[ModelStallHeatmapEntry]:
        agent_stalls: dict[str, Counter[str]] = defaultdict(Counter)

        for entry in entries:
            if (
                "stall" not in entry.entry_type
                and "stall" not in entry.description.lower()
            ):
                continue
            agent = entry.agent_id or "unknown"
            if entry.ticket_id:
                agent_stalls[agent][entry.ticket_id] += 1
            else:
                agent_stalls[agent]["_no_ticket"] += 1

        return [
            ModelStallHeatmapEntry(
                agent_id=agent,
                stall_count=sum(counts.values()),
                affected_tickets=sorted(t for t in counts if t != "_no_ticket"),
            )
            for agent, counts in sorted(
                agent_stalls.items(),
                key=lambda x: sum(x[1].values()),
                reverse=True,
            )
        ]

    @staticmethod
    def _parse_recorded_at(recorded_at: str) -> datetime | None:
        try:
            return datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
        except ValueError:
            return None

    @staticmethod
    def _normalize_failure_key(description: str) -> str:
        first_line = description.split("\n")[0][:120]
        return first_line.strip()

    def _render_rollup(
        self,
        command: ModelCompactorCommand,
        entries: list[ModelAuditEntry],
        failure_modes: list[ModelFailureMode],
        recurring_tickets: list[ModelRecurringTicket],
        stall_heatmap: list[ModelStallHeatmapEntry],
    ) -> str:
        now = datetime.now(tz=UTC).isoformat()
        lines = [
            f"# Audit Trail Rollup — {command.lookback_days}-day window",
            "",
            f"**Generated**: {now}",
            f"**Total entries**: {len(entries)}",
            f"**Lookback**: {command.lookback_days} days",
            "",
            "## Top Failure Modes",
            "",
        ]
        if failure_modes:
            for fm in failure_modes:
                tickets = f" ({len(fm.ticket_ids)} tickets)" if fm.ticket_ids else ""
                lines.append(
                    f"- **{fm.description}** — {fm.count} occurrences{tickets}"
                )
        else:
            lines.append("(no failures found)")
        lines.append("")

        if recurring_tickets:
            lines.extend(["## Recurring Tickets", ""])
            for rt in recurring_tickets:
                lines.append(
                    f"- **{rt.ticket_id}** — {rt.failure_count} failures (last: {rt.last_failure})"
                )
            lines.append("")

        if stall_heatmap:
            lines.extend(["## Stall Heatmap", ""])
            for sh in stall_heatmap:
                tickets = ", ".join(sh.affected_tickets[:5]) or "(none)"
                lines.append(
                    f"- **{sh.agent_id}** — {sh.stall_count} stalls: {tickets}"
                )
            lines.append("")

        return "\n".join(lines)


__all__: list[str] = [
    "AuditReader",
    "FileSystemAuditReader",
    "FileSystemRollupWriter",
    "HandlerAuditTrailCompactor",
    "RollupWriter",
]
