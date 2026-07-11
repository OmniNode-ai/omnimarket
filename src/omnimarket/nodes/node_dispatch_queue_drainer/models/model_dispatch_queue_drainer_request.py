# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request for one dispatch-queue-drainer run."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, field_validator


class ModelDispatchQueueDrainerRequest(BaseModel):
    """Input for one ``node_dispatch_queue_drainer`` run.

    All fields are optional so the handler can fall back to its documented
    environment-derived defaults (``ONEX_STATE_DIR`` / ``OMNI_HOME``) and
    oldest-item queue scanning when the caller doesn't supply an explicit
    value. ``limit`` is validated here (construction time) rather than
    inside ``handle()`` — fail-fast over a defensive late guard.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    queue_item_path: Path | None = None
    queue_dir: Path | None = None
    limit: int = 1
    state_dir: Path | None = None
    tasks_dir: Path | None = None
    omni_home: Path | None = None

    @field_validator("limit")
    @classmethod
    def validate_limit(cls, value: int) -> int:
        if value != 1:
            raise ValueError("first drainer slice supports limit=1 only")
        return value


__all__: list[str] = ["ModelDispatchQueueDrainerRequest"]
