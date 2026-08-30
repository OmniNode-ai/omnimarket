# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request for one dispatch-queue-drainer run."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelDispatchQueueDrainerRequest(BaseModel):
    """Input for one ``node_dispatch_queue_drainer`` run.

    All path fields are optional so the handler can fall back to its documented
    environment-derived defaults (``ONEX_STATE_DIR`` / ``OMNI_HOME``) and
    oldest-selectable-item queue scanning when the caller doesn't supply an
    explicit value. ``limit`` is validated here (construction time) rather than
    inside ``handle()`` — fail-fast over a defensive late guard.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    queue_item_path: Path | None = None
    queue_dir: Path | None = None
    limit: int = 1
    state_dir: Path | None = None
    tasks_dir: Path | None = None
    omni_home: Path | None = None
    #: OMN-17018: a real dry run. Selection, validation and command compilation
    #: still happen; no lifecycle transition, no dispatch record and no result
    #: artifact are written, and the dispatch worker is never invoked. Before
    #: this the flag was a phantom the request model did not even accept.
    dry_run: bool = False
    #: Renewable claim lease. Expiry marks the claim STALE for observers; it
    #: never deletes the queue item and never silently returns it to QUEUED.
    claim_lease_seconds: int = Field(default=900, gt=0)
    #: How long a DISPATCHED item may go unacknowledged before it is observably
    #: PENDING. It is never counted as processed and never re-selected as
    #: untouched either way.
    dispatch_ack_timeout_seconds: int = Field(default=900, gt=0)
    #: Recorded on every lifecycle transition this run writes.
    actor: str = Field(default="node_dispatch_queue_drainer", min_length=1)

    @field_validator("limit")
    @classmethod
    def validate_limit(cls, value: int) -> int:
        if value != 1:
            raise ValueError("first drainer slice supports limit=1 only")
        return value


__all__: list[str] = ["ModelDispatchQueueDrainerRequest"]
