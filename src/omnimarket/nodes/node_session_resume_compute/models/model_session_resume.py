# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result of session recovery: dangling CLAIMs, closing rows and relaunch CLAIMs.

Every operation runs under a session scope bound through the team identity declaration. A member
session lists every dangling CLAIM but marks another principal's as ``foreign``, runs the reconciler
in report mode only, and is refused a closing TERMINAL, a relaunch CLAIM or a landing notice for any
CLAIM its own principals did not write (or that is not the child of one of its own lanes).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

from omnimarket.models.session_scope import ModelIdentityClaim, ModelSessionScope

Stamp = Annotated[
    str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
]
Lane = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")]
OneLine = Annotated[str, StringConstraints(pattern=r"^[^|\n]+$")]
Owner = Literal["own", "foreign"]
Operation = Literal["dangling", "close", "relaunch"]


class ModelCloseSpec(BaseModel):
    """The TERMINAL that closes one stopped lane's CLAIM on its behalf (session-resume step 3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: Lane  # the resuming session's own lane, written as closed-by=
    dead_lane: Lane
    dead_claim: str  # the stopped CLAIM's stamp or its LCT1 claim token
    mode: Literal["after_msg", "landed", "released"]
    after_msg: str | None = None
    ticket: str | None = None
    pr: str | None = None
    squash: str | None = None
    reason: str | None = None
    friction: OneLine = "none"


class ModelRelaunchSpec(BaseModel):
    """The CLAIM template a relaunched lane writes for itself (session-resume step 4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dead_lane: Lane
    dead_claim: str
    dead_parent: str | None = None
    landing_lane: Lane
    dispatcher: Lane  # the resuming session's own lane
    ticket: str
    new_lane: Lane | None = None
    actor: str = "<executor, e.g. claude:sonnet5:subagent>"
    t0: str = "<T0>"
    scope: str = "re-read head, worktree and old rows before any write"
    est_hours: str = "1"
    displaces: str = "nothing queued"
    repo: str | None = None
    pr: str | None = None
    worktree: str | None = None
    consent: str | None = None


class ModelSessionResumeRequest(BaseModel):
    """One session recovery operation over the rolling ledger and its archive rolls."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Operation
    ledger_path: str
    identity_claims: tuple[ModelIdentityClaim, ...] = ()
    overlay_roots: tuple[str, ...] | None = None
    since: Stamp | None = None
    t0: Stamp | None = None
    dry_run: bool = False
    now: Stamp | None = None
    close: ModelCloseSpec | None = None
    relaunch: ModelRelaunchSpec | None = None


class ModelDanglingClaim(BaseModel):
    """One CLAIM no later row closes, with the principal that wrote it and whose it is."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_stamp: str
    lane: str
    state: Literal["QUIET", "ACTIVE", "UNKNOWN"]
    last_row: str
    source: str
    ticket: str
    repo: str
    pr: str
    worktree: str
    parent: str
    consent: str
    principal: str
    owner: Owner


class ModelSessionResumeResult(BaseModel):
    """``status`` ``ok`` with the listing or the composed row; ``refused`` with the reason and no row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Operation
    status: Literal["ok", "refused", "error"]
    scope: ModelSessionScope
    reconciler_mode: Literal["apply", "report"]
    dangling: tuple[ModelDanglingClaim, ...] = ()
    prs: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    summary: str = ""
    route: Literal["relaunch", "landing-lane"] | None = None
    row: str | None = None
    reason: str | None = None
