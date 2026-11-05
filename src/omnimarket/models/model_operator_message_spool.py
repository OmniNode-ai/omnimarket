# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Standard-library types also available inside the installed hook package."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class ModelOperatorSpoolStatus:
    depth: int | None
    oldest_age_seconds: float | None
    bound_seconds: float
    state: Literal["OK", "FINDING"]
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ModelHookMessageRequest:
    event: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ModelHookMessageResult:
    status: ModelOperatorSpoolStatus
    appended: int = 0


@dataclass(frozen=True, slots=True)
class ModelOperatorArrivalPolicy:
    spool_root_env: str
    state_root_env: str
    state_subdir: str
    spool_subdir: str
    append_command_env: str
    append_timeout_seconds: float
    stale_after_seconds: float
    drain_interval_seconds: int
    lane: str
    source: str
    fallback_state_root_env: str
    drain_label: str
    max_appends_per_hook: int
    max_appends_per_drain: int


@dataclass(frozen=True, slots=True)
class ModelOperatorSpoolUnitRequest:
    platform: Literal["launchd", "systemd"]
    python: str
    copy_root: str
    environment: dict[str, str]
    overlay_path: str | None = None


@dataclass(frozen=True, slots=True)
class ModelOperatorSpoolUnitResult:
    files: dict[str, str]
