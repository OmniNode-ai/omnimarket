# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Terminal result artifact model for dispatch queue drainer runs."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_dispatch_queue_phase import EnumDispatchQueuePhase
from omnimarket.enums.enum_dispatch_terminal_reason import (
    EnumDispatchTerminalDisposition,
    EnumDispatchTerminalReason,
)


class ModelDispatchQueueDrainerResult(BaseModel):
    """Result of one queue-drainer run, with the item's lifecycle phase.

    ``status`` describes what this *run* did; ``lifecycle_phase`` describes
    where the *item* now is. They are deliberately separate: a ``compiled`` run
    leaves the item ``DISPATCHED`` and awaiting acknowledgement, which is not
    the same thing as processed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["compiled", "blocked", "empty", "dry_run"]
    queue_item_path: str = ""
    result_artifact_path: str = ""
    blocked_reason: str = ""
    dispatch_worker_command: dict[str, object] | None = None
    dispatch_worker_result: dict[str, object] | None = None
    processed_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    #: Phase the queue item holds after this run. ``None`` only for ``empty``
    #: (no item was selected) and for ``dry_run`` (nothing was transitioned).
    lifecycle_phase: EnumDispatchQueuePhase | None = None
    lifecycle_record_path: str = ""
    terminal_disposition: EnumDispatchTerminalDisposition | None = None
    terminal_reason: EnumDispatchTerminalReason | None = None
    #: True when the run mutated nothing — no lifecycle transition, no dispatch
    #: record, no result artifact.
    dry_run: bool = False

    @property
    def auto_redispatchable(self) -> bool:
        """Whether recovery policy may auto-redispatch this item.

        ``deliberate_cancellation`` and ``unknown`` are refused by construction
        (the policy lives on :class:`EnumDispatchTerminalReason`); a run that
        did not reach a stopped terminal is not a redispatch candidate at all.
        """
        if self.terminal_reason is None:
            return False
        return self.terminal_reason.auto_redispatchable


__all__: list[str] = ["ModelDispatchQueueDrainerResult"]
