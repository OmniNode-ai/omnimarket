# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Durable lifecycle ledger for legacy dispatch queue items (OMN-17018).

This is the queue's **progress operator**: the surface that makes a selected
item provably transition instead of being merely re-read. It is append-only,
it never moves or deletes a queue file, and an expired claim lease marks the
item stale rather than returning it to the selectable set (operator ruling
A1-REVISED — leases are renewable, expiry marks stale, never deletes).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from omnimarket.enums.enum_dispatch_queue_phase import EnumDispatchQueuePhase
from omnimarket.enums.enum_dispatch_terminal_reason import (
    EnumDispatchTerminalDisposition,
    EnumDispatchTerminalReason,
)
from omnimarket.nodes.node_dispatch_queue_drainer.models.model_dispatch_queue_lifecycle import (
    ModelDispatchQueueLifecycle,
    ModelDispatchQueueTerminal,
    ModelDispatchQueueTransition,
)

#: Ledger location, relative to the resolved state dir.
LIFECYCLE_DIRNAME = "lifecycle"


@runtime_checkable
class ProtocolDispatchQueueLifecycleLedger(Protocol):
    """Effect boundary the drainer transitions queue items through."""

    def load(self, queue_item_path: Path) -> ModelDispatchQueueLifecycle | None: ...

    def claim(
        self,
        queue_item_path: Path,
        *,
        actor: str,
        lease_seconds: int,
        now: datetime,
    ) -> ModelDispatchQueueLifecycle: ...

    def renew(
        self,
        queue_item_path: Path,
        *,
        actor: str,
        lease_seconds: int,
        now: datetime,
    ) -> ModelDispatchQueueLifecycle: ...

    def mark_dispatched(
        self,
        queue_item_path: Path,
        *,
        actor: str,
        ack_timeout_seconds: int,
        now: datetime,
        detail: str,
    ) -> ModelDispatchQueueLifecycle: ...

    def acknowledge_started(
        self,
        queue_item_path: Path,
        *,
        actor: str,
        now: datetime,
        detail: str,
    ) -> ModelDispatchQueueLifecycle: ...

    def mark_terminal(
        self,
        queue_item_path: Path,
        *,
        actor: str,
        terminal: ModelDispatchQueueTerminal,
        now: datetime,
        detail: str,
    ) -> ModelDispatchQueueLifecycle: ...

    def record_path(self, queue_item_path: Path) -> Path: ...


class InvalidLifecycleTransitionError(RuntimeError):
    """Raised when a caller asks for a transition the lifecycle does not allow."""


class FileDispatchQueueLifecycleLedger:
    """Filesystem implementation of :class:`ProtocolDispatchQueueLifecycleLedger`.

    One JSON file per queue item under
    ``<state_dir>/dispatch_queue/lifecycle/<item_id>.json``. Writes are
    append-only over the ``transitions`` tuple: no prior transition is ever
    edited or dropped, so the record is the item's replayable history.
    """

    def __init__(self, lifecycle_dir: Path) -> None:
        self._lifecycle_dir = lifecycle_dir

    def record_path(self, queue_item_path: Path) -> Path:
        return self._lifecycle_dir / f"{queue_item_path.stem}.json"

    def load(self, queue_item_path: Path) -> ModelDispatchQueueLifecycle | None:
        path = self.record_path(queue_item_path)
        if not path.is_file():
            return None
        return ModelDispatchQueueLifecycle.model_validate_json(
            path.read_text(encoding="utf-8")
        )

    def claim(
        self,
        queue_item_path: Path,
        *,
        actor: str,
        lease_seconds: int,
        now: datetime,
    ) -> ModelDispatchQueueLifecycle:
        """Move a QUEUED item to CLAIMED under a renewable lease."""
        existing = self.load(queue_item_path)
        if existing is not None:
            raise InvalidLifecycleTransitionError(
                f"queue item {queue_item_path.stem!r} is already at phase "
                f"{existing.phase.value!r}; only an unclaimed item can be claimed"
            )
        transition = ModelDispatchQueueTransition(
            phase=EnumDispatchQueuePhase.CLAIMED,
            occurred_at=now,
            actor=actor,
            detail="claimed for draining",
            lease_expires_at=_lease_deadline(now, lease_seconds),
        )
        return self._append(queue_item_path, transition, previous=None)

    def renew(
        self,
        queue_item_path: Path,
        *,
        actor: str,
        lease_seconds: int,
        now: datetime,
    ) -> ModelDispatchQueueLifecycle:
        """Extend the lease on an in-flight item without changing its phase.

        Renewal is the reason expiry is safe to treat as *stale* rather than
        *dead*: a live attempt keeps its claim by renewing, so an expired lease
        is real evidence that nothing is driving the item.
        """
        previous = self._require(queue_item_path)
        if not previous.is_in_flight():
            raise InvalidLifecycleTransitionError(
                f"queue item {queue_item_path.stem!r} is at phase "
                f"{previous.phase.value!r}; only an in-flight item holds a lease"
            )
        latest = previous.latest
        transition = ModelDispatchQueueTransition(
            phase=latest.phase,
            occurred_at=now,
            actor=actor,
            detail="lease renewed",
            lease_expires_at=_lease_deadline(now, lease_seconds),
            ack_deadline=latest.ack_deadline,
        )
        return self._append(queue_item_path, transition, previous=previous)

    def mark_dispatched(
        self,
        queue_item_path: Path,
        *,
        actor: str,
        ack_timeout_seconds: int,
        now: datetime,
        detail: str,
    ) -> ModelDispatchQueueLifecycle:
        """Record that a compiled command was handed off, with an ack deadline."""
        previous = self._require(queue_item_path)
        self._require_phase(previous, EnumDispatchQueuePhase.CLAIMED, "dispatch")
        transition = ModelDispatchQueueTransition(
            phase=EnumDispatchQueuePhase.DISPATCHED,
            occurred_at=now,
            actor=actor,
            detail=detail,
            lease_expires_at=previous.latest.lease_expires_at,
            ack_deadline=_lease_deadline(now, ack_timeout_seconds),
        )
        return self._append(queue_item_path, transition, previous=previous)

    def acknowledge_started(
        self,
        queue_item_path: Path,
        *,
        actor: str,
        now: datetime,
        detail: str,
    ) -> ModelDispatchQueueLifecycle:
        """Record the dispatched lane's acknowledgement that it started."""
        previous = self._require(queue_item_path)
        self._require_phase(previous, EnumDispatchQueuePhase.DISPATCHED, "acknowledge")
        transition = ModelDispatchQueueTransition(
            phase=EnumDispatchQueuePhase.STARTED,
            occurred_at=now,
            actor=actor,
            detail=detail,
            lease_expires_at=previous.latest.lease_expires_at,
        )
        return self._append(queue_item_path, transition, previous=previous)

    def mark_terminal(
        self,
        queue_item_path: Path,
        *,
        actor: str,
        terminal: ModelDispatchQueueTerminal,
        now: datetime,
        detail: str,
    ) -> ModelDispatchQueueLifecycle:
        """Close the item with a typed disposition and, when stopped, a reason."""
        previous = self.load(queue_item_path)
        if previous is not None and previous.phase is EnumDispatchQueuePhase.TERMINAL:
            raise InvalidLifecycleTransitionError(
                f"queue item {queue_item_path.stem!r} is already TERMINAL"
            )
        transition = ModelDispatchQueueTransition(
            phase=EnumDispatchQueuePhase.TERMINAL,
            occurred_at=now,
            actor=actor,
            detail=detail,
            terminal=terminal,
        )
        return self._append(queue_item_path, transition, previous=previous)

    def _require(self, queue_item_path: Path) -> ModelDispatchQueueLifecycle:
        existing = self.load(queue_item_path)
        if existing is None:
            raise InvalidLifecycleTransitionError(
                f"queue item {queue_item_path.stem!r} has no lifecycle record; "
                "it must be claimed before it can be transitioned"
            )
        return existing

    def _require_phase(
        self,
        lifecycle: ModelDispatchQueueLifecycle,
        expected: EnumDispatchQueuePhase,
        action: str,
    ) -> None:
        if lifecycle.phase is not expected:
            raise InvalidLifecycleTransitionError(
                f"queue item {lifecycle.item_id!r} is at phase "
                f"{lifecycle.phase.value!r}; {action} requires {expected.value!r}"
            )

    def _append(
        self,
        queue_item_path: Path,
        transition: ModelDispatchQueueTransition,
        *,
        previous: ModelDispatchQueueLifecycle | None,
    ) -> ModelDispatchQueueLifecycle:
        history = () if previous is None else previous.transitions
        lifecycle = ModelDispatchQueueLifecycle(
            queue_item_path=str(queue_item_path),
            item_id=queue_item_path.stem,
            transitions=(*history, transition),
        )
        out_path = self.record_path(queue_item_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(lifecycle.model_dump(mode="json"), indent=2) + "\n",
            encoding="utf-8",
        )
        return lifecycle


def blocked_terminal(reason: EnumDispatchTerminalReason) -> ModelDispatchQueueTerminal:
    """Build a STOPPED terminal disposition for *reason*."""
    return ModelDispatchQueueTerminal(
        disposition=EnumDispatchTerminalDisposition.STOPPED, reason=reason
    )


def _lease_deadline(now: datetime, seconds: int) -> datetime:
    if seconds <= 0:
        raise ValueError("lease/ack window must be a positive number of seconds")
    return datetime.fromtimestamp(now.timestamp() + seconds, tz=UTC)


__all__: list[str] = [
    "LIFECYCLE_DIRNAME",
    "FileDispatchQueueLifecycleLedger",
    "InvalidLifecycleTransitionError",
    "ProtocolDispatchQueueLifecycleLedger",
    "blocked_terminal",
]
