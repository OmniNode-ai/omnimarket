# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the Claude token pool is asked to do, and what it answers.

No model here dumps a token: the pasted ``claude setup-token`` output and the
resolved token are ``SecretStr``, and the result's ``token`` is excluded from
every dump, so a result written as JSON names labels and fingerprints only.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class ModelClaudeTokenPoolRequest(BaseModel):
    """One operation on this host's pool.

    ``add``: store the token found in ``setup_token_output`` under ``label``.
    ``resolve``: choose the first token, in the order they were added, whose
    limit has passed and whose OAuth login is not revoked. ``mark_limited``:
    the account behind ``label`` hit its usage limit, out for ``seconds``.
    ``mark_revoked``: skip ``label`` until re-added. ``status``: read the pool.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["add", "resolve", "mark_limited", "mark_revoked", "status"]
    label: str | None = None
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


class ModelClaudeTokenPoolResult(BaseModel):
    """The pool after the operation, the token ``resolve`` chose and any new ``mark_revoked`` alert."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["add", "resolve", "mark_limited", "mark_revoked", "status"]
    host: str
    tokens: tuple[ModelClaudeTokenPoolEntry, ...] = ()
    chosen: str | None = None
    token: SecretStr | None = Field(default=None, exclude=True, repr=False)
    limited: int = 0
    revoked: tuple[str, ...] = ()
    alert: str | None = None
    all_limited: bool = False
    next_reset: str | None = None
    status_line: str


__all__ = [
    "ModelClaudeTokenPoolEntry",
    "ModelClaudeTokenPoolRequest",
    "ModelClaudeTokenPoolResult",
]
