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

from omnimarket.nodes.node_claude_pool_revoke_staleness_compute.models.model_revoke_staleness import (
    ModelClearRevokeCommand,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_rotation import (
    ModelRotationMark,
)


class ModelPoolLabelLanes(BaseModel):
    """How many lanes are running under one pool label, as the caller of ``resolve`` counted them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(min_length=1)
    lanes: int = Field(ge=0)


class ModelClaudeTokenPoolRefusal(BaseModel):
    """Why ``resolve`` handed a new lane start no token although the pool holds a usable one.

    ``label_lane_cap``: every usable label already runs the overlay's cap of lanes. ``lane_counts_missing``:
    the overlay declares a cap and the caller passed no count to hold it against. ``lane_cap_unread``: the
    caller passed a count and the overlay that holds the cap could not be read on this host. Labels and
    numbers only, never a token.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: Literal["label_lane_cap", "lane_counts_missing", "lane_cap_unread"]
    #: The overlay's lanes-per-label cap, when it was read.
    cap: int | None = None
    #: The usable labels that are at the cap.
    labels: tuple[str, ...] = ()
    detail: str


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
    ``release``; ``pinned_by`` says whether an operator's assign or the spread rule set it. ``clear_revokes``:
    execute the typed ``clear_commands`` the revoke staleness compute decided (see ``clear_commands``).

    ``resolve`` for a NEW lane start also takes ``drain_labels`` (the usage verdict's list of labels close
    to their limit: skipped while any other label is usable, used only when nothing else is) and
    ``lane_counts`` (the lanes running under each label, as the caller counted them, which the overlay's
    lanes-per-label cap is held against).
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
        "clear_revokes",
    ]
    label: str | None = None
    #: ``pin``: who sets the pin. An operator's ``assign`` pin is never replaced by the spread rule.
    pinned_by: Literal["assign", "spread"] = "assign"
    marks: tuple[ModelRotationMark, ...] = ()
    #: ``clear_revokes``: the typed commands the staleness compute decided. The pool clears a label's revoked mark only
    #: when it still holds that fingerprint AND that exact ``revoked_at``; a newer revoke, a re-added token or an
    #: already cleared mark is left alone.
    clear_commands: tuple[ModelClearRevokeCommand, ...] = ()
    labels: tuple[str, ...] = ()
    #: ``sync_marks``: when the source wrote ``marks``. A local limit or revocation observed after this
    #: instant is newer than the snapshot and is kept; unset, the marks are authoritative.
    marks_at: datetime | None = None
    #: Decide on the pool as the operation would leave it, and write neither the pool nor the status mirror.
    dry_run: bool = False
    setup_token_output: SecretStr | None = None
    seconds: float | None = Field(default=None, gt=0)
    force: bool = False
    #: ``resolve``: labels the usage verdict asks NEW lane starts to stay off. The name and the element
    #: type are the verdict node's result field (``ModelPoolUsageResult.drain_labels``); a seam test pins both.
    drain_labels: tuple[str, ...] = ()
    #: ``resolve``: the lanes running under each label. ``None`` means the caller counted nothing, which
    #: differs from an empty tuple (the caller counted, and no lane is running): a declared cap cannot be
    #: held against a count nobody made.
    lane_counts: tuple[ModelPoolLabelLanes, ...] | None = None


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
    revoked_at: str | None = None
    limited_at: str | None = None


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
        "clear_revokes",
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
    #: ``clear_revokes``: the labels whose revoked mark was cleared.
    cleared: tuple[str, ...] = ()
    alert: str | None = None
    all_limited: bool = False
    next_reset: str | None = None
    status_line: str
    #: ``resolve``: set when a usable token exists and the start was refused all the same.
    refusal: ModelClaudeTokenPoolRefusal | None = None
    #: ``resolve``: the chosen label was in ``drain_labels`` and nothing else was usable.
    drained_fallback: bool = False


__all__ = [
    "ModelClaudeTokenPoolEntry",
    "ModelClaudeTokenPoolRefusal",
    "ModelClaudeTokenPoolRequest",
    "ModelClaudeTokenPoolResult",
    "ModelPoolLabelLanes",
]
