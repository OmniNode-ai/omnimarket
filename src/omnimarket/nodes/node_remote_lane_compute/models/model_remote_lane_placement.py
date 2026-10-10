# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Requests and results of the remote-lane placement decision (OMN-20669).

A host reading arrives already evaluated: the admission bar, the Codex bar, the lane
slots and the free capacity are the placement reader's numbers for that host at read
time. This node chooses among them; it reads no host and holds no host identity.

The dispatch venv's lock hash of the launching host (the request) and of each host
(its reading) arrive the same way (OMN-20862). When they differ and that drift alone
keeps every host from taking the lane, the result is VENV_DRIFT with one reconcile
intent, never a bare no-host.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

_FROZEN = ConfigDict(frozen=True, extra="forbid")

CODEX_ENGINE = "codex"

RECONCILE_SCRIPT = "omnibase_infra/scripts/reconcile-workspace-venvs.sh"


class EnumRemoteLaneOutcome(StrEnum):
    """How a placement ended: admitted, no host, or no host because of dispatch-venv state."""

    ADMITTED = "ADMITTED"
    NO_HOST = "NO_HOST"
    VENV_DRIFT = "VENV_DRIFT"
    # A hash the drift check needs was not supplied: not a pass, not a drift.
    VENV_UNKNOWN = "VENV_UNKNOWN"


class ModelRemoteLaneHostReading(BaseModel):
    """One pool host as the placement reader saw it."""

    model_config = _FROZEN

    name: str = Field(min_length=1)
    local: bool = False
    engines: tuple[str, ...] = ()
    lane_admission_refusal: str | None = None
    codex_refusal: str | None = None
    lane_slots: int = 0
    lane_cap: int = 0
    placed: int = Field(default=0, ge=0)
    # None: the reader could not rank the host (an unreadable host, -inf to the reader,
    # which a JSON bus envelope cannot carry as a number). It ranks below every host.
    rank_free: float | None = 0.0
    mem_avail_gb: float = 0.0
    # The lock hash of this host's dispatch venv. None: the reader did not read it (an old
    # producer); on a drift-judged request that is VENV_UNKNOWN, never a pass.
    dispatch_venv_hash: str | None = Field(default=None, min_length=1)


class ModelRemoteLanePlacementRequest(BaseModel):
    """The lane's engine, an optional pinned host, the live host marks and every reading."""

    model_config = _FROZEN

    engine: str = Field(min_length=1)
    pinned_host: str | None = None
    limited_hosts: tuple[str, ...] = ()
    auth_expired_hosts: tuple[str, ...] = ()
    readings: tuple[ModelRemoteLaneHostReading, ...] = ()
    # The launching host's dispatch-venv lock hash: the hash every host must hold. Drift is
    # judged when it, any reading's hash, or ``dispatch_venv_required`` is present; a request
    # with none of them is an old producer's and places as before.
    launching_venv_hash: str | None = Field(default=None, min_length=1)
    dispatch_venv_required: bool = False


class ModelRemoteLaneHostVerdict(BaseModel):
    """Why one host did or did not take the lane."""

    model_config = _FROZEN

    host: str
    reason: str


class ModelRemoteLaneHostVenv(BaseModel):
    """One host's dispatch-venv lock hash as read, None when it was not read."""

    model_config = _FROZEN

    host: str
    hash: str | None = None


class ModelRemoteLaneVenvDrift(BaseModel):
    """Both sides of a dispatch-venv drift: the launching host's hash and each blocked host's."""

    model_config = _FROZEN

    launching_hash: str | None
    hosts: tuple[ModelRemoteLaneHostVenv, ...]


class ModelRemoteLaneReconcileIntent(BaseModel):
    """The one reconcile to run for a drift: the script, the hosts it repairs, the hash to reach.

    An intent only: this node runs nothing. A consumer of the decided event runs it once.
    """

    model_config = _FROZEN

    script: str = RECONCILE_SCRIPT
    hosts: tuple[str, ...]
    target_hash: str | None


class ModelRemoteLanePlacementResult(BaseModel):
    """The chosen host and engine, or none, with a verdict for every reading."""

    model_config = _FROZEN

    host: str | None
    engine: str
    local: bool = False
    verdicts: tuple[ModelRemoteLaneHostVerdict, ...] = ()
    outcome: EnumRemoteLaneOutcome = EnumRemoteLaneOutcome.NO_HOST
    venv_drift: ModelRemoteLaneVenvDrift | None = None
    reconcile: ModelRemoteLaneReconcileIntent | None = None
