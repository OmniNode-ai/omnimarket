# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelAgentAuthorizationGrant — session-scoped tool authorization grant.

Written by node_authorize, read by Task 3 of the unused-hooks plan
(OMN-9087, PermissionRequest authorization gate in omniclaude).

The file-on-disk shape is the contract between the writer (this node) and
the reader (the omniclaude hook). The schema mirrors the type spec in
``docs/plans/2026-04-17-unused-hooks-applications.md`` (Task 3).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

AUTHORIZATION_FILE_RELATIVE_PATH = "session/authorization.json"


class ModelAgentAuthorizationGrant(BaseModel):
    """Session-scoped grant authorizing a set of tools over a scope.

    ``scope``       — glob-style paths the grant covers (e.g. ``src/**``).
    ``granted_at``  — UTC datetime the grant was written.
    ``expires_at``  — UTC datetime after which the grant is invalid;
                      ``None`` means non-expiring (explicitly opted in).
    ``tools``       — tool names the grant applies to (e.g. ``Edit``, ``Write``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: tuple[str, ...] = Field(..., min_length=1)
    granted_at: datetime
    expires_at: datetime | None
    tools: tuple[str, ...] = Field(..., min_length=1)

    @field_validator("granted_at", "expires_at")
    @classmethod
    def _require_timezone_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError(
                "timestamps must be timezone-aware; a naive datetime would "
                "break is_expired comparison against datetime.now(UTC)"
            )
        return value

    def is_expired(self, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (now or datetime.now(UTC)) >= self.expires_at


def load_grant_if_valid(
    path: Path, now: datetime | None = None
) -> ModelAgentAuthorizationGrant | None:
    """Load a grant from disk; return ``None`` if missing, malformed, or expired.

    This is the reader-side contract used by Task 3 hook logic. Missing file,
    malformed JSON, schema mismatch, and past ``expires_at`` all collapse to
    the same "no grant" outcome by design — callers make a single decision.
    """
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        grant = ModelAgentAuthorizationGrant.model_validate(data)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if grant.is_expired(now):
        return None
    return grant
