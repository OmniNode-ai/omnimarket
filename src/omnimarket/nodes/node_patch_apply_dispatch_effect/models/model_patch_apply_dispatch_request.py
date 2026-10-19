# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelPatchApplyDispatchRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    correlation_id: UUID = Field(default_factory=uuid4)
    operation: Literal["tick"] = "tick"
    now: datetime
    registry_root: Path
    ledger_path: Path
    state_dir: Path
    # Registry clone names a patch can belong to (repos.yaml); the clone is read under registry_root.
    repos: tuple[str, ...] = ()
    # Where lanes leave patches; None means registry_root/.onex_state/hook-preflight.
    patch_root: Path | None = None
    reports_dir: Path | None = None
    # A patch older than this is closed out unapplied.
    max_age_hours: float = Field(default=48.0, gt=0)
    # A live CLAIM in this window holds the patch.
    claim_window_minutes: float = Field(default=45.0, gt=0)
    # A dispatched patch is dispatched again no sooner than this, unless its bytes changed.
    redispatch_hours: float = Field(default=6.0, gt=0)
    max_dispatch: int = Field(default=3, ge=1)
    # False reads and reports; nothing is dispatched, moved or appended.
    apply: bool = True

    @field_validator("now")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("now must have a timezone")
        return value

    @field_validator("repos")
    @classmethod
    def safe_repos(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            not name or Path(name).name != name or name in {".", ".."} for name in value
        ):
            raise ValueError("repo must be a directory name")
        return value
