# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the Claude token pool is asked to do, and what it answers.

No model here dumps a token: the pasted ``claude setup-token`` output and the
resolved token are ``SecretStr``, and the result's ``token`` is excluded from
every dump, so a result written as JSON names labels and fingerprints only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_rotation import (
    ModelRotationMark,
)


class ModelClaudeTokenPoolRequest(BaseModel):
    """One operation on this host's pool.

    ``add``: store the token found in ``setup_token_output`` under ``label``.
    ``resolve``: choose the first token, in the order they were added, whose
    limit has passed and whose OAuth login is not revoked. ``mark_limited``:
    the account behind ``label`` hit its usage limit, out for ``seconds``.
    ``mark_revoked``: skip ``label`` until re-added. ``reorder``: draw ``labels`` first, in that
    order, then every other token in its present order (a draw order differs per host so no two hosts
    draw the same first token). ``status``: read the pool. ``record_use``: a lane on this host
    started under ``label``; count the use and when it was seen, so the spread rule can tell a
    login that has been in use from one no lanes have used yet. ``resolve`` counts the use of the token
    it hands out. ``pin``: keep ``label`` first in this host's draw, through every ``reorder``, until
    ``release``; ``pinned_by`` says whether an operator's assign or the spread rule set it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal[
        "add",
        "resolve",
        "mark_limited",
        "mark_revoked",
        "reorder",
        "sync_marks",
        "status",
        "record_use",
        "pin",
        "release",
    ]
    label: str | None = None
    #: ``pin``: who sets the pin. An operator's ``assign`` pin is never replaced by the spread rule.
    pinned_by: Literal["assign", "spread"] = "assign"
    marks: tuple[ModelRotationMark, ...] = ()
    labels: tuple[str, ...] = ()
    #: ``sync_marks``: when the source wrote ``marks``. A local limit or revocation observed after this
    #: instant is newer than the snapshot and is kept; unset, the marks are authoritative.
    marks_at: datetime | None = None
    #: Decide on the pool as the operation would leave it, and write neither the pool nor the status mirror.
    dry_run: bool = False
    setup_token_output: SecretStr | None = None
    seconds: float | None = Field(default=None, gt=0)
    force: bool = False


class ModelClaudeTokenPoolEntry(BaseModel):
    """One token of the pool: its account label and fingerprint, never its value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    fingerprint: str
    added_at: str
    until: str | None = None
    limited: bool = False
    revoked: bool = False
    #: How many times a lane on this host started under the token, and when the last one did.
    uses: int = 0
    last_used_at: str | None = None


class ModelClaudeTokenPoolResult(BaseModel):
    """The pool after the operation, the token ``resolve`` chose and any new ``mark_revoked`` alert."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal[
        "add",
        "resolve",
        "mark_limited",
        "mark_revoked",
        "reorder",
        "sync_marks",
        "status",
        "record_use",
        "pin",
        "release",
    ]
    host: str
    tokens: tuple[ModelClaudeTokenPoolEntry, ...] = ()
    chosen: str | None = None
    #: The label held first in the draw until released, and who pinned it (``assign`` or ``spread``).
    pinned: str | None = None
    pinned_by: Literal["assign", "spread"] | None = None
    token: SecretStr | None = Field(default=None, exclude=True, repr=False)
    limited: int = 0
    revoked: tuple[str, ...] = ()
    synced: tuple[str, ...] = ()
    alert: str | None = None
    all_limited: bool = False
    next_reset: str | None = None
    status_line: str


__all__ = [
    "ModelClaudeTokenPoolEntry",
    "ModelClaudeTokenPoolRequest",
    "ModelClaudeTokenPoolResult",
]
