# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Close eligible released lease claims once, recording refused writes for retry."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

DEFAULT_LIMIT = 40


def utc_stamp() -> str:
    """The ledger's stamp for now: a row carries its own append time."""
    return datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


class ModelLeaseClaimCloseRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)
    claims: tuple[dict[str, Any], ...]
    live_lease_ids: frozenset[int]
    revoked_lease_ids: frozenset[int]
    released_this_tick: frozenset[int]
    read_at: str
    state_dir: Path
    writer: Callable[[str], tuple[bool, str]]
    limit: int | None = DEFAULT_LIMIT
    dry_run: bool = False
    clock: Callable[[], str] = utc_stamp
    cause_ticket: str | None = None
    fallback_ticket: str | None = None


class ModelLeaseClaimCloseResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)
    rows: tuple[str, ...]
    closed: tuple[str, ...]
    failed: tuple[str, ...]
    skipped_live: int
    skipped_recent: int
    skipped_closed: int
    left: int

    def line(self) -> str:
        return f"LEASE-CLAIM-CLOSE closed={len(self.closed)} failed={len(self.failed)} skipped_live={self.skipped_live} skipped_recent={self.skipped_recent} left={self.left} skipped_closed={self.skipped_closed}"
