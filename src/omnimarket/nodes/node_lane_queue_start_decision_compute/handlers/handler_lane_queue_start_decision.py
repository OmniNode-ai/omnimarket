# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Decide queue starts against the hosts each lane can actually use.

A lane pinned to a full host was relaunched against pool-wide free slots.
The runner's holdback also counted queued higher-class lanes that could not
use the slot. Candidate capacity and relaunch-window claimants are evaluated
here together, using only the caller's facts and time.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from omnimarket.nodes.node_lane_queue_start_decision_compute.models.model_lane_queue_start_decision import (
    ModelLaneQueueHeld,
    ModelLaneQueueStart,
    ModelLaneQueueStartRequest,
    ModelLaneQueueStartResult,
    ModelQueuedLane,
)


def _stamp(value: str | None) -> datetime | None:
    """Read an ISO-Z stamp without letting a bad ledger cell stop the queue."""
    if value is None:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def _enqueue_key(entry: ModelQueuedLane) -> tuple[bool, datetime, str]:
    stamp = _stamp(entry.enqueued_at)
    return stamp is None, stamp or datetime.max.replace(tzinfo=UTC), entry.lane


def _effective_pin(
    entry: ModelQueuedLane, now: datetime | None, pin_s: float
) -> str | None:
    if not entry.pin:
        return None
    if entry.pin_strict:
        return entry.pin
    since = _stamp(entry.pin_since)
    if (
        now is not None
        and since is not None
        and 0 <= (now - since).total_seconds() < pin_s
    ):
        return entry.pin
    return None


def _relaunch_due(
    entry: ModelQueuedLane, now: datetime | None, relaunch_s: float
) -> bool:
    launched = _stamp(entry.launched_at)
    if launched is None or now is None or launched > now:
        return True
    updated = _stamp(entry.updated_at)
    if updated is not None and updated > launched:
        return True
    return (now - launched).total_seconds() >= relaunch_s


class HandlerLaneQueueStartDecision:
    """Spend eligible host slots in priority order without stale queue claims."""

    def handle(self, request: ModelLaneQueueStartRequest) -> ModelLaneQueueStartResult:
        """Return deterministic starts and holds without changing the input facts."""
        now = _stamp(request.now)
        unique: dict[str, ModelQueuedLane] = {}
        for entry in request.entries:
            unique.setdefault(entry.lane, entry)
        ranks = {name: index for index, name in enumerate(request.priority_classes)}
        last_rank = max(len(request.priority_classes) - 1, 0)

        def rank(priority_class: str) -> int:
            return ranks.get(priority_class, last_rank)

        entries = sorted(
            unique.values(),
            key=lambda entry: (rank(entry.priority_class), *_enqueue_key(entry)),
        )
        remaining: dict[str, dict[str, int]] = {
            host.name: {
                "claude": max(0, host.claude_slots),
                "codex": max(0, host.codex_slots),
            }
            for host in request.hosts
            if host.probe_error is None
        }
        all_unread = bool(request.hosts) and not remaining
        queue_markers = {f"lane-{name}" for name in unique}
        claimed = {
            unit.priority_class
            for unit in request.waiting_units
            if unit.unit not in queue_markers
        }
        starts: list[ModelLaneQueueStart] = []
        held: list[ModelLaneQueueHeld] = []
        for entry in entries:
            if not _relaunch_due(entry, now, request.relaunch_s):
                held.append(
                    ModelLaneQueueHeld(
                        lane=entry.lane,
                        reason_code="relaunch-window",
                        detail=f"Relaunch window of {request.relaunch_s:g}s has not elapsed",
                    )
                )
                claimed.add(entry.priority_class)
                continue
            engine: Literal["claude", "codex"] = (
                "codex" if entry.model == "codex" else "claude"
            )
            pin = _effective_pin(entry, now, request.pin_s)
            candidates = [
                name
                for name, slots in remaining.items()
                if slots[engine] >= 1 and (pin is None or name == pin)
            ]
            if not candidates:
                if all_unread:
                    refusal = ModelLaneQueueHeld(
                        lane=entry.lane,
                        reason_code="probe-unread",
                        detail="Every host probe is unread",
                    )
                elif pin is not None:
                    slots = remaining.get(pin, {}).get(engine, 0)
                    refusal = ModelLaneQueueHeld(
                        lane=entry.lane,
                        reason_code="pinned-host-full",
                        detail=f"Pinned host {pin} has {slots} remaining {engine} slots",
                    )
                else:
                    refusal = ModelLaneQueueHeld(
                        lane=entry.lane,
                        reason_code="no-free-slot",
                        detail=f"No readable host has a free {engine} slot",
                    )
                held.append(refusal)
                continue
            above = sorted(
                (name for name in claimed if rank(name) < rank(entry.priority_class)),
                key=lambda name: (rank(name), name),
            )
            candidate_slot_total = sum(remaining[name][engine] for name in candidates)
            if 0 < candidate_slot_total <= len(above):
                held.append(
                    ModelLaneQueueHeld(
                        lane=entry.lane,
                        reason_code="priority-holdback",
                        detail=f"Candidate slots reserved for higher classes: {', '.join(above)}",
                    )
                )
                continue
            chosen = min(candidates, key=lambda name: (-remaining[name][engine], name))
            remaining[chosen][engine] -= 1
            starts.append(
                ModelLaneQueueStart(
                    lane=entry.lane, engine=engine, host=chosen, pinned=pin is not None
                )
            )

        oldest = min(entries, key=_enqueue_key) if entries else None
        enqueued = _stamp(oldest.enqueued_at) if oldest is not None else None
        wait = (
            int((now - enqueued).total_seconds())
            if now is not None and enqueued is not None
            else 0
        )
        return ModelLaneQueueStartResult(
            starts=starts,
            held=held,
            depth=len(entries),
            oldest_lane=oldest.lane if oldest is not None else None,
            oldest_wait_s=wait,
        )
