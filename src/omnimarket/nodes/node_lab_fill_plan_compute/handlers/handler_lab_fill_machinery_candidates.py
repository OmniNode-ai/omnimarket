# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Turn the factory's own breaks into lab-fill candidates (OMN-22413).

Three sources, in the policy's source order: a FRICTION class (grouped by class, guard and reason)
that recurs at least the policy's floor within its window and is not fixed; a throughput-tick key
printed MISSING or UNKNOWN on at least the policy's floor of distinct ticks; and an open ticket whose
description carries the policy's ``Class:`` line. A class a live CLAIM names by its key, or a
machinery ticket a live CLAIM holds, is skipped as ``owned:<lane>``. Every candidate carries the policy's
priority class. The handler reads facts only: no file, network or clock.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta

from ..models.model_lab_fill_machinery import (
    ModelLabFillMachineryCandidate,
    ModelLabFillMachineryCandidatesRequest,
    ModelLabFillMachineryCandidatesResult,
    ModelLabFillMachineryClaim,
    ModelLabFillMachineryDiagnostic,
    ModelLabFillMachineryFrictionRow,
    ModelLabFillMachineryPolicy,
    ModelLabFillMachinerySkip,
)
from .helpers_machinery_policy import machinery_policy

_TICKET_RE = re.compile(r"OMN-[0-9]+")
_DONE_STATES = ("completed", "canceled")
TITLE_LIMIT = 120


def _when(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _owner(
    claims: tuple[ModelLabFillMachineryClaim, ...],
    key: str,
    parts: list[str],
    ticket: str,
) -> str:
    """The lane of the first live CLAIM that names the class key or holds its ticket."""
    for claim in claims:
        text = claim.text.lower()
        names = key.lower() in text or (
            bool(parts) and all(p.lower() in text for p in parts)
        )
        if names or (ticket and claim.ticket == ticket):
            return claim.lane
    return ""


class _Build:
    def __init__(self, request: ModelLabFillMachineryCandidatesRequest) -> None:
        self.request = request
        self.policy: ModelLabFillMachineryPolicy = request.policy or machinery_policy()
        self.skipped: list[ModelLabFillMachinerySkip] = []
        self.drops: Counter[str] = Counter()
        self.seen = 0
        self.tickets = {t.identifier: t for t in request.tickets}

    def _offer(self, key: str, parts: list[str], ticket: str) -> bool:
        """Count the class as seen; False, with the skip recorded, when a live CLAIM owns it."""
        self.seen += 1
        lane = _owner(self.request.claims, key, parts, ticket)
        if lane:
            self.skipped.append(
                ModelLabFillMachinerySkip(id=key, reason=f"owned:{lane}")
            )
            self.drops["owned"] += 1
        return not lane

    def _candidate(self, **fields: object) -> ModelLabFillMachineryCandidate:
        return ModelLabFillMachineryCandidate(
            priority_class=self.policy.priority_class,
            **fields,
        )

    def friction(self) -> list[ModelLabFillMachineryCandidate]:
        policy = self.policy
        now = _when(self.request.observed_at)
        if now is None:
            self.drops["bad-observed-at"] += 1
            return []
        since = now - timedelta(hours=policy.window_hours)
        groups: dict[
            tuple[str, str, str],
            list[tuple[datetime, ModelLabFillMachineryFrictionRow]],
        ] = defaultdict(list)
        for row in self.request.friction_rows:
            ts = _when(row.ts)
            if ts is None:
                self.drops["bad-ts"] += 1
            elif ts >= since:
                groups[(row.class_name, row.guard, row.reason)].append((ts, row))
        found: list[tuple[int, datetime, ModelLabFillMachineryCandidate]] = []
        for (class_name, guard, reason), rows in groups.items():
            parts = [p for p in (class_name, guard, reason) if p]
            if not parts:
                continue
            key = "friction:" + ":".join(parts)
            newest_ts, newest = max(rows, key=lambda r: r[0])
            ticket = next(
                (
                    t
                    for t in (newest.ticket, newest.existing)
                    if _TICKET_RE.fullmatch(t)
                ),
                "",
            )
            if len(rows) < policy.friction_recurrence_floor:
                self.seen += 1
                self.drops["below-floor"] += 1
                continue
            facts = self.tickets.get(ticket)
            completed = (
                _when(facts.completed_at) if facts and facts.completed_at else None
            )
            if (
                facts
                and facts.state_type in _DONE_STATES
                and completed
                and completed >= newest_ts
            ):
                self.seen += 1
                self.drops["done"] += 1
                continue
            if not self._offer(key, parts, ""):
                continue
            lanes = sorted({r.lane for _, r in rows if r.lane})
            found.append(
                (
                    len(rows),
                    newest_ts,
                    self._candidate(
                        source="friction-class",
                        machinery_key=key,
                        ticket=ticket,
                        title=f"Recurring FRICTION class {' / '.join(parts)}"[
                            :TITLE_LIMIT
                        ],
                        evidence=f"{len(rows)} FRICTION rows in {policy.window_hours} h, newest {newest.ts}"
                        + (f", lanes {','.join(lanes)}" if lanes else ""),
                        updated_at=newest.ts,
                    ),
                )
            )
        found.sort(key=lambda f: (-f[0], -f[1].timestamp(), f[2].machinery_key))
        return [c for _, _, c in found]

    def ticks(self) -> list[ModelLabFillMachineryCandidate]:
        policy = self.policy
        ticks: dict[str, set[str]] = defaultdict(set)
        statuses: dict[str, set[str]] = defaultdict(set)
        for line in self.request.tick_lines:
            if line.status in policy.tick_statuses:
                ticks[line.key].add(line.tick)
                statuses[line.key].add(line.status)
        found: list[ModelLabFillMachineryCandidate] = []
        for key, seen_on in sorted(ticks.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            if len(seen_on) < policy.tick_recurrence_floor:
                self.seen += 1
                self.drops["below-floor"] += 1
            elif self._offer(f"tick:{key}", [key], ""):
                found.append(
                    self._candidate(
                        source="tick-key",
                        machinery_key=f"tick:{key}",
                        title=f"Throughput tick key {key} reads {'/'.join(sorted(statuses[key]))}"[
                            :TITLE_LIMIT
                        ],
                        evidence=f"{'/'.join(sorted(statuses[key]))} on {len(seen_on)} ticks",
                    )
                )
        return found

    def tickets_of_class(self) -> list[ModelLabFillMachineryCandidate]:
        line = re.compile(
            rf"^\s*Class:\s*{re.escape(self.policy.ticket_class)}\s*$",
            re.IGNORECASE | re.MULTILINE,
        )
        found: list[ModelLabFillMachineryCandidate] = []
        for ticket in sorted(self.request.tickets, key=lambda t: _number(t.identifier)):
            if not line.search(ticket.description):
                continue
            if ticket.state_type in _DONE_STATES:
                self.seen += 1
                self.drops["done"] += 1
            elif ticket.has_children:
                self.seen += 1
                self.drops["has-children"] += 1
            elif self._offer(f"ticket:{ticket.identifier}", [], ticket.identifier):
                found.append(
                    self._candidate(
                        source="machinery-ticket",
                        machinery_key=f"ticket:{ticket.identifier}",
                        ticket=ticket.identifier,
                        title=ticket.title[:TITLE_LIMIT],
                        evidence=f"{ticket.identifier} carries Class: {self.policy.ticket_class}",
                        labels=ticket.labels,
                        updated_at=ticket.updated_at,
                        project_id=ticket.project_id,
                        assignee_id=ticket.assignee_id,
                    )
                )
        return found


def _number(identifier: str) -> int:
    match = re.fullmatch(r"OMN-([0-9]+)", identifier)
    return int(match.group(1)) if match else 0


class HandlerLabFillMachineryCandidates:
    """Classify the factory-break facts into machinery candidates, skipping what a lane owns."""

    def handle(
        self, request: ModelLabFillMachineryCandidatesRequest
    ) -> ModelLabFillMachineryCandidatesResult:
        build = _Build(request)
        sources = {
            "friction-class": build.friction,
            "tick-key": build.ticks,
            "machinery-ticket": build.tickets_of_class,
        }
        candidates = [c for name in build.policy.source_order for c in sources[name]()]
        return ModelLabFillMachineryCandidatesResult(
            candidates=tuple(candidates),
            skipped=tuple(build.skipped),
            diagnostic=ModelLabFillMachineryDiagnostic(
                seen=build.seen,
                eligible=len(candidates),
                reasons=",".join(
                    f"{k}:{n}"
                    for k, n in sorted(
                        build.drops.items(), key=lambda kv: (-kv[1], kv[0])
                    )
                ),
            ),
        )
