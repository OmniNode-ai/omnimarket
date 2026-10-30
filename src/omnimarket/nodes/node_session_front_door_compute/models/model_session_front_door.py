# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result of the session front door node.

Operations:

* ``start``: the session-start entry. Picks the preflight profile from the team identity
  declaration (``operator`` only for a positive operator match, ``team`` otherwise), runs the onex
  plugin's preflight runner with it, asserts the receipt's required checks, decides the host mode,
  reads the ledger digest and inbox counts, appends the session CLAIM and writes ``session.env``.
* ``profile``: the profile a session gets, and why.
* ``assert_receipt``: the required-check assertion on a preflight receipt for the session's profile.
* ``host_mode``: ``coordination``, ``bus-member``, ``detached`` or ``unknown`` from a receipt.
* ``name``, ``init``, ``show``, ``lanes``, ``boundary``, ``claim_row``, ``status_row`` and
  ``terminal_row``: the session file and row operations.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from omnimarket.models.session_scope import ModelIdentityClaim, ModelSessionScope

Operation = Literal[
    "start",
    "profile",
    "assert_receipt",
    "host_mode",
    "name",
    "init",
    "show",
    "lanes",
    "boundary",
    "claim_row",
    "status_row",
    "terminal_row",
]
Profile = Literal["operator", "team"]
HostMode = Literal["coordination", "bus-member", "detached", "unknown"]
LaneState = Literal["dispatched", "handed-off", "terminal"]
Status = Literal["ok", "blocked", "refused", "error"]

HOST_MODES: tuple[str, ...] = ("coordination", "bus-member", "detached", "unknown")
LANE_STATES: tuple[str, ...] = ("dispatched", "handed-off", "terminal")


class ModelSessionRowSpec(BaseModel):
    """The cells of one CLAIM, STATUS or TERMINAL row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ts: str
    lane: str
    text: str
    ticket: str | None = None
    actor: str | None = None
    model: str | None = None
    scope: str | None = None
    est_hours: float | None = None
    displaces: str | None = None
    kv: tuple[str, ...] = ()
    t0: str | None = None
    closes_claim: str | None = None
    outcome: str | None = None
    friction: str | None = None


class ModelSessionStartSpec(BaseModel):
    """``start``: what the session CLAIM says, and where the preflight runner and ledger are.

    ``preflight_script`` defaults to ``ONEX_PREFLIGHT_SCRIPT``, then the ``onex`` plugin's
    ``scripts/session_preflight.py`` in ``$HOME/.claude/plugins/installed_plugins.json``.
    ``ledger_command`` (the ``onex-ledger`` argv) defaults to ``ONEX_LEDGER_COMMAND``; there is no
    other default.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket: str
    est_hours: float
    displaces: str
    actor: str
    model: str
    scope: str
    purpose: str | None = None
    lane: str | None = None
    text: str = "Orchestration session."
    hours: int = 12
    preflight_script: str | None = None
    preflight_overlay: str | None = None
    python: str = "python3"
    ledger_command: tuple[str, ...] | None = None
    preflight_timeout_seconds: int = 300
    ledger_timeout_seconds: int = 90


class ModelSessionFrontDoorRequest(BaseModel):
    """One operation. ``environment`` ``None`` reads the process environment.

    ``sessions_root`` defaults to ``$OMNI_HOME/.claude_scratch/sessions``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Operation
    identity_claims: tuple[ModelIdentityClaim, ...] = ()
    overlay_roots: tuple[str, ...] | None = None
    environment: dict[str, str] | None = None
    sessions_root: str | None = None
    receipt: str | None = None
    expected_overlay_sha256: str | None = None
    receipt_max_age_seconds: int = 3600
    lane: str | None = None
    purpose: str | None = None
    ticket: str | None = None
    claim_ts: str | None = None
    host_mode: HostMode | None = None
    state: LaneState | None = None
    digest: str | None = None
    dry_run: bool = False
    row: ModelSessionRowSpec | None = None
    start: ModelSessionStartSpec | None = None


class ModelReceiptAssertion(BaseModel):
    """What the required-check assertion found in one receipt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    profile: Profile
    required: tuple[str, ...]
    ran: tuple[str, ...]
    missing: tuple[str, ...]
    failed: tuple[str, ...]
    overlay_sha256: str | None
    findings: tuple[str, ...]


class ModelHostModeVerdict(BaseModel):
    """The host mode and, for anything short of the best mode for the profile, why.

    ``bus-member`` is a startup verdict from the receipt and the bus configuration; it precedes the
    first CLAIM and is never proof that the machine is attached.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: HostMode
    profile: Profile
    reasons: tuple[str, ...] = ()


class ModelSessionFrontDoorResult(BaseModel):
    """``exit_code`` keeps the entry's codes: 0 ok, 1 a preflight blocker, 2 refused, 3 taken."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Operation
    status: Status
    exit_code: int
    scope: ModelSessionScope
    profile: Profile
    profile_reason: str = ""
    lane: str | None = None
    claim_ts: str | None = None
    receipt: str | None = None
    preflight_argv: tuple[str, ...] = ()
    assertion: ModelReceiptAssertion | None = None
    host_mode: ModelHostModeVerdict | None = None
    session_env: str | None = None
    lines: tuple[str, ...] = ()
    rule: str | None = None
    message: str = ""
    fix: str | None = None
