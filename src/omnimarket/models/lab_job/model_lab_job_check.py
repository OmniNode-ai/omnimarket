# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The check sweep's request and its per-job result, shared with the orchestrator.

``ModelLabJobChecked`` is a record of what the sweep read. It carries every
input of a liveness verdict and of the done rule so the orchestrator can map it
to reducer events and a replay can be audited; it decides nothing itself.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_lab_job import EnumLabJobLiveness, EnumLabJobState
from omnimarket.enums.enum_lab_job_check import (
    EnumLabJobCheckScope,
    EnumLabJobDeadline,
)
from omnimarket.models.liveness import EnumEvidenceBasis, EnumRelayState


class ModelLabJobCheckRequested(BaseModel):
    """``onex.cmd.omnimarket.lab-job-check-requested.v1``: one sweep is due.

    ``check_id`` names the schedule window, so a replayed tick asks for the same
    sweep; ``requested_at`` is the tick's own clock and the sweep's only ``now``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    check_id: str = Field(min_length=1)
    scope: EnumLabJobCheckScope = EnumLabJobCheckScope.NONTERMINAL
    requested_at: datetime


class ModelLabJobLedgerEvidence(BaseModel):
    """The job's CLAIM row and the TERMINAL row that closes it, if one does.

    ``terminal_*`` is set only for a TERMINAL newer than the CLAIM; an older
    TERMINAL of a reused lane name is not evidence about this dispatch.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    claim_row_id: str = Field(min_length=1)
    claimed_at: datetime
    terminal_row_id: str | None = None
    terminal_at: datetime | None = None
    terminal_outcome: str | None = None


class ModelLabJobLivenessEvidence(BaseModel):
    """A liveness verdict and every input it was computed from.

    ``lane_attributed`` is per lane: whether this lane has any lane-attributed
    hook event since its CLAIM, not whether the window had any.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EnumLabJobLiveness
    evidence_basis: EnumEvidenceBasis
    reason: str = Field(min_length=1)
    relay_state: EnumRelayState | None = None
    relay_last_event_at: datetime | None = None
    last_hook_event_at: datetime | None = None
    last_event_age_s: int | None = Field(default=None, ge=0)
    hook_event_count: int = Field(default=0, ge=0)
    lane_attributed: bool = False
    silence_threshold_s: int = Field(default=0, ge=0)


class ModelLabJobPrObservation(BaseModel):
    """A pull request's state as last observed, at its head, with the read time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    target: str = Field(min_length=1)
    found: bool
    state: str | None = None
    head_sha: str | None = None
    ci_verdict: str | None = None
    red_contexts: tuple[str, ...] = ()
    pending_contexts: tuple[str, ...] = ()
    merged_at: str | None = None
    read_at: datetime | None = None


class ModelLabJobChecked(BaseModel):
    """``onex.evt.omnimarket.lab-job-checked.v1``: one job, one sweep."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    check_id: str = Field(min_length=1)
    job_id: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    job_state: EnumLabJobState
    checked_at: datetime
    elapsed_deadlines: tuple[EnumLabJobDeadline, ...] = ()
    verdict: EnumLabJobLiveness | None = None
    ledger: ModelLabJobLedgerEvidence | None = None
    liveness: ModelLabJobLivenessEvidence | None = None
    pr_observations: tuple[ModelLabJobPrObservation, ...] = ()


class ModelLabJobCheckSweep(BaseModel):
    """What one sweep did: the per-job records it emitted, in job order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    check_id: str = Field(min_length=1)
    checked_at: datetime
    checked: tuple[ModelLabJobChecked, ...] = ()
    published: int = Field(default=0, ge=0)


__all__: list[str] = [
    "ModelLabJobCheckRequested",
    "ModelLabJobCheckSweep",
    "ModelLabJobChecked",
    "ModelLabJobLedgerEvidence",
    "ModelLabJobLivenessEvidence",
    "ModelLabJobPrObservation",
]
