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

from omnimarket.models.model_claude_pool_limit_recheck import (
    ModelLimitRecheckCommand,
)
from omnimarket.models.model_claude_pool_revoke_recheck import (
    ModelRevokeRecheckCommand,
)
from omnimarket.models.model_claude_pool_revoke_staleness import (
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
    ``mark_revoked``: skip ``label`` until re-added; ``cause`` says why the login is out — ``revoked``
    (a real revoke, the default) or ``challenged`` (a 403 challenge page, out of rotation like a revoke
    until the pool tick re-tests it and clears the mark when it answers). ``reorder``: draw ``labels``
    first, in that
    order, then every other token in its present order (a draw order differs per host so no two hosts
    draw the same first token). ``status``: read the pool. ``record_use``: a lane on this host
    started under ``label``; count the use and when it was seen, so the spread rule can tell a
    login that has been in use from one no lanes have used yet. ``resolve`` counts the use of the token
    it hands out. ``pin``: keep ``label`` first in this host's draw, through every ``reorder``, until
    ``release``; ``pinned_by`` says whether an operator's assign or the spread rule set it. ``clear_revokes``:
    execute the typed ``clear_commands`` the revoke staleness compute decided (see ``clear_commands``).
    ``apply_limit_rechecks``: execute the typed ``limit_commands`` the limit recheck compute decided after a probe
    (see ``limit_commands``). ``apply_revoke_rechecks``: execute the typed ``revoke_commands`` the revoke recheck
    compute decided after a probe of a revoked label (see ``revoke_commands``). ``export``: hand the stored
    token of ``label`` to the caller's pipe, for adopting a host-held label into the secret store; the value
    is never in a result, only in ``token``.

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
        "apply_limit_rechecks",
        "apply_revoke_rechecks",
        "export",
    ]
    label: str | None = None
    #: ``mark_revoked``: why the login is out. A challenge is out of rotation like a revoke until the
    #: pool tick re-tests the label and clears the mark when it answers.
    cause: Literal["revoked", "challenged"] = "revoked"
    #: ``pin``: who sets the pin. An operator's ``assign`` pin is never replaced by the spread rule.
    pinned_by: Literal["assign", "spread"] = "assign"
    marks: tuple[ModelRotationMark, ...] = ()
    #: ``clear_revokes``: the typed commands the staleness compute decided. The pool clears a label's revoked mark only
    #: when it still holds that fingerprint AND that exact ``revoked_at``; a newer revoke, a re-added token or an
    #: already cleared mark is left alone.
    clear_commands: tuple[ModelClearRevokeCommand, ...] = ()
    #: ``apply_limit_rechecks``: the typed commands the limit recheck compute decided after a probe answered. The pool
    #: executes a command only while it still holds that fingerprint AND that exact limited-until stamp.
    limit_commands: tuple[ModelLimitRecheckCommand, ...] = ()
    #: ``apply_revoke_rechecks``: the typed commands the revoke recheck compute decided after a probe answered. The pool
    #: executes a command only while it still holds that fingerprint AND that exact revoked stamp.
    revoke_commands: tuple[ModelRevokeRecheckCommand, ...] = ()
    labels: tuple[str, ...] = ()
    #: ``sync_marks``: when the source wrote ``marks``. A local limit or revocation observed after this
    #: instant is newer than the snapshot and is kept; unset, the marks are authoritative.
    marks_at: datetime | None = None
    #: Decide on the pool as the operation would leave it, and write neither the pool nor the status mirror.
    dry_run: bool = False
    setup_token_output: SecretStr | None = None
    seconds: float | None = Field(default=None, gt=0)
    #: ``mark_limited``: ``seconds`` comes from the reset a limit notice itself names, not from a backoff estimate.
    #: An exact notice replaces the label's stamp (a newer notice wins even when earlier); an estimate never shortens one.
    exact: bool = False
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
    #: When the limit recheck last got an answer from a probe of this label (an error answer records none).
    probed_at: str | None = None
    #: Why the login is out (``revoked_at`` set): ``revoked``, a real revoke, or ``challenged``, a 403
    #: challenge page the pool tick re-tests. ``None`` (or a stored entry without the key, from before
    #: this field existed) reads as ``revoked``.
    revoke_cause: Literal["revoked", "challenged"] | None = None


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
        "apply_limit_rechecks",
        "apply_revoke_rechecks",
        "export",
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
    #: The labels whose out mark is a 403 challenge, not a real revoke. They are also in ``revoked``,
    #: so every existing reader keeps treating them as out until the recheck clears the mark.
    challenged: tuple[str, ...] = ()
    synced: tuple[str, ...] = ()
    #: ``clear_revokes``: the labels whose revoked mark was cleared.
    cleared: tuple[str, ...] = ()
    #: ``apply_limit_rechecks``: labels whose limited mark was cleared, whose stamp was replaced by a newer
    #: reset, and whose probe time was recorded with the mark kept.
    limits_cleared: tuple[str, ...] = ()
    limits_replaced: tuple[str, ...] = ()
    limits_kept: tuple[str, ...] = ()
    #: ``apply_revoke_rechecks``: labels whose revoked mark was cleared, whose cause was confirmed as a
    #: real revoke, and whose probe time was recorded with the mark kept.
    revoke_rechecks_cleared: tuple[str, ...] = ()
    revoke_rechecks_confirmed: tuple[str, ...] = ()
    revoke_rechecks_kept: tuple[str, ...] = ()
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
