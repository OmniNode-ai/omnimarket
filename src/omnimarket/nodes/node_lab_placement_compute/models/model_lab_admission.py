# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the pre-placement admission: live host state read before a lane is placed (OMN-26214).

Placement used to act on prose: a Codex lane was placed though no RULING allowed it, a lane was pinned to a
host that failed plugin parity, a lane was placed on a host whose registry is read-only (Errno 30 on the
reconcile lock), and a third lane was placed after two had hit the same errno on the same host. The request
carries what was read (the host table's rows, the parity dry-run state, the lock probe reading, the ledger's
FRICTION and MERGE rows); the result is one verdict with the evidence of every refusal.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: The checks, in the order the admission runs them.
AdmissionCheck = Literal[
    "allowed_models", "parity_dry_run", "reconcile_lock_probe", "same_errno"
]


class ModelAdmissionRules(BaseModel):
    """The ``admission`` block of the node contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The engine whose models need the brief's ``codexOk`` and a RULING that allows it.
    gated_engine: str = Field(..., min_length=1)
    #: Minutes before ``now`` that a same-errno FRICTION row still counts.
    repeat_window_minutes: int = Field(..., gt=0)
    #: Same-errno rows in the window at which a class or host is refused until a MERGE row clears it.
    repeat_threshold: int = Field(..., ge=2)
    #: Errnos of the reconcile lock probe that refuse a host (30 is EROFS, the read-only registry).
    refused_lock_errnos: tuple[int, ...] = Field(..., min_length=1)


class ModelAdmissionHost(BaseModel):
    """One candidate host and what was read of it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(..., min_length=1)
    #: The host table row's ``codex`` column: the host may run Codex.
    codex: bool = False
    #: The parity dry-run: ``ok``, ``modified`` (the installed plugin differs from its shipped revision) or
    #: ``unread`` (no dry-run answer).
    parity: Literal["ok", "modified", "unread"] = "unread"
    #: What the lane sandbox's reconcile lock probe read: the errno, or None when the lock is takeable.
    lock_probe_errno: int | None = None
    #: The probe ran (a None errno from a probe that did not run is no evidence the lock is takeable).
    lock_probe_read: bool = False


class ModelAdmissionRequest(BaseModel):
    """One lane to place, the hosts to choose from and the live state read for them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(..., min_length=1)
    #: The lane's class; omitted, the lane name without its trailing ``-<HHMM>`` tag.
    lane_class: str | None = None
    #: The model the lane asks for; omitted, the lane model policy's ceiling.
    model: str | None = None
    #: The brief carries ``codexOk``.
    brief_codex_ok: bool = False
    #: The latest RULING allows Codex, and the row that says so (cited in the verdict).
    ruling_allows_codex: bool = False
    ruling_ref: str = ""
    pin: str | None = None
    #: The brief requires the pinned host: it is refused, never dropped.
    pin_required: bool = False
    #: Hosts whose plugin parity the lane's task repairs: it is never pinned to one nor placed on it.
    repairs_parity_of: tuple[str, ...] = ()
    hosts: tuple[ModelAdmissionHost, ...]
    #: Raw rolling-ledger rows (FRICTION rows, and the MERGE rows that clear them), as appended.
    ledger_rows: tuple[str, ...] = ()
    now: datetime

    @model_validator(mode="after")
    def _distinct_hosts(self) -> ModelAdmissionRequest:
        names = [host.name for host in self.hosts]
        if len(set(names)) != len(names):
            raise ValueError("hosts repeats a host name")
        if self.now.tzinfo is None:
            raise ValueError("now must carry a timezone")
        return self


class ModelAdmissionRefusal(BaseModel):
    """One refusal, with the evidence it rests on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    check: AdmissionCheck
    #: The host or class refused; ``lane`` when the whole lane is.
    subject: str
    reason: str
    evidence: tuple[str, ...] = ()


class ModelAdmissionHostVerdict(BaseModel):
    """Whether one host may take the lane."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    eligible: bool
    reasons: tuple[str, ...] = ()


class ModelAdmissionResult(BaseModel):
    """The verdict: the model, the pin kept (None: place unpinned), the hosts that may take the lane."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    admitted: bool
    model: str | None
    pin: str | None
    #: Why the caller's pin was dropped and the lane placed unpinned; None when it was kept or never given.
    pin_dropped: str | None
    eligible_hosts: tuple[str, ...]
    hosts: tuple[ModelAdmissionHostVerdict, ...]
    refusals: tuple[ModelAdmissionRefusal, ...]


class ModelLockProbeRequest(BaseModel):
    """The directory the reconcile lock would be created in, as the lane sandbox sees it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lock_dir: str = Field(..., min_length=1)


class ModelLockProbeReading(BaseModel):
    """What the read-only probe saw. It never opened, created or locked the lock file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lock_dir: str
    #: The errno creating the lock would raise (30: read-only filesystem, 13: no write permission, 2: no such
    #: directory); None when it would succeed.
    errno: int | None
    detail: str
