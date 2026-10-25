# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed operations for Claude account limit recovery (OMN-20864)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal


@dataclass(frozen=True)
class ModelClaudeAccountLimitRequest:
    operation: Literal["mark", "pool", "revoke", "auth", "probe", "passed", "reprobe"]
    hosts: tuple[Any, ...] = ()
    interval_s: float = 900.0
    directory: Path | None = None
    host: str = ""
    text: str = ""
    seconds: float = 0
    now: datetime | None = None
    lease_id: int | None = None
    observed_at: datetime | None = None
    capture: Any = None
    state: Path | None = None
    account: str | None = None
    probe_host: Any = None
    decisions: dict[str, dict[str, Any]] | None = None


@dataclass(frozen=True)
class ModelClaudeAccountLimitResult:
    limited: dict[str, str] = field(default_factory=dict)
    until: str | None = None
    has_room: bool = False
    account: str | None = None
    alert: str | None = None
    failure: dict[str, Any] = field(default_factory=dict)
    identities: frozenset[str] = field(default_factory=frozenset)
    recovered_at: datetime | None = None
