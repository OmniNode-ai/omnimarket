# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ticket closer's candidate, verdict and result models, and the schedule's overlay (OMN-24138)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.runtime_proof.model_runtime_proof_close import (
    ModelProofCloseRequest,
)


class EnumCloserAction(StrEnum):
    """What one scheduled closer run does with one candidate ticket."""

    FLIPPED_DONE = "flipped_done"
    HELD = "held"
    VERIFY_NOT_RUN = "verify_not_run"
    WRITE_REFUSED = "write_refused"


class EnumCloserReadingSource(StrEnum):
    """The reading profile the schedule binds: where the closer's readings come from."""

    UNREAD = "unread"
    CONTRACT_CLONES = "contract_clones"


class ModelTicketCloserScheduleOverlay(BaseModel):
    """The closer schedule's overlay: the cadence of the scheduled run and where its readings come from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_interval_seconds: int = Field(gt=0)
    max_candidates_per_run: int = Field(gt=0)
    lane: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$",
        description="The lane identity the closer's ticket writes carry.",
    )
    execution_audience: Literal["hosted", "local_done_gate"] = Field(
        description="The execution audience the DoD verifier runs against.",
    )
    done_state: str = Field(
        min_length=1,
        description="The ticket state the closer moves a flipped ticket into.",
    )
    reading_source: EnumCloserReadingSource = Field(
        description="The reading profile the schedule binds: unread (in-memory default) or a bound profile.",
    )
    home_env: str | None = Field(
        default=None,
        description=(
            "The environment variable whose value is the registry root path the bound "
            "profile's clone readings sit under; None keeps the in-memory defaults."
        ),
    )
    bus_lane: str | None = Field(
        default=None,
        description=(
            "The declared bus lane the closer's ticket calls publish on; None keeps the "
            "in-memory defaults."
        ),
    )
    ticket_timeout_seconds: float | None = Field(
        default=None,
        gt=0,
        description=(
            "How long one ticket bus call waits for the ticket write node's terminal "
            "event; None keeps the in-memory defaults."
        ),
    )
    scan_limit: int | None = Field(
        default=None,
        gt=0,
        description=(
            "How many contract ids one run's candidate reading scans at most; None "
            "keeps the in-memory defaults."
        ),
    )


class ModelCloserCandidate(BaseModel):
    """One candidate ticket as the source read it, with the facts the flip predicate decides on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(pattern=r"^OMN-\d+$")
    criteria: tuple[str, ...]
    all_prs_merged: bool
    runtime_proof: ModelProofCloseRequest | None = None
    prior_reversal: bool = False
    outcome_changed_since_reversal: bool = False


class ModelCloserVerifyReading(BaseModel):
    """One run of the DoD verifier for one ticket: what it counted, or why it could not run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str
    ran: bool
    error_code: str = ""
    error_detail: str = ""
    total_checks: int = 0
    verified_count: int = 0
    failed_count: int = 0
    non_probative_count: int = 0
    behavior_proving_count: int = 0
    bound_criteria: tuple[str, ...] = ()
    head_revision: str = ""
    exercises_running_system: bool = False


class ModelCloserWriteReceipt(BaseModel):
    """What the ticket write node reported after one move to done."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    reason: str = ""


class ModelCloserTicketOutcome(BaseModel):
    """One candidate ticket's disposition in one scheduled run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str
    action: EnumCloserAction
    reason: str = ""
    unmet: tuple[str, ...] = ()


class ModelTicketCloserResult(BaseModel):
    """One scheduled closer run: what it examined and what it flipped, as the runtime publishes it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tick_id: str
    computed_at: datetime
    candidates_read: bool
    examined: int
    flipped: int
    outcomes: tuple[ModelCloserTicketOutcome, ...]
    # Why the reading was unread when candidates_read is False; empty on a read run.
    unread_reason: str = ""


__all__ = [
    "EnumCloserAction",
    "EnumCloserReadingSource",
    "ModelCloserCandidate",
    "ModelCloserTicketOutcome",
    "ModelCloserVerifyReading",
    "ModelCloserWriteReceipt",
    "ModelTicketCloserResult",
    "ModelTicketCloserScheduleOverlay",
]
