# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The live J8 run (OMN-23087): its overlay, command, subject and result.

The overlay holds every setting of the run; nothing here has a default, so an absent key refuses the load and names it.
The subject is one real pull request as the collector read it: the facts the landing decision judges and the facts the
check poster (piece 8) judges. They are the same facts; only an injected break makes them differ.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    ModelLandingFacts,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_join_j8 import (
    EnumJoinJ8Outcome,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_lab_check import (
    EnumLabCheckVerdict,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_state import (
    PR_KEY_PATTERN,
)

OVERLAY_ID = "node_pr_landing_join_j8"


class EnumJ8Want(StrEnum):
    """Which end of the join a run is to exercise: the golden (a head the decision merges), the error (a head the lab
    check holds), or whichever real head comes first."""

    GOLDEN = "golden"
    ERROR = "error"
    ANY = "any"


class EnumJ8Break(StrEnum):
    """The named injected breaks of the join, its failing controls."""

    BLIND_DECISION = "blind-decision"
    SILENT_LANDING = "silent-landing"


class ModelJoinJ8LiveOverlay(BaseModel):
    """Every setting of the live run, read from ``<root>/node_pr_landing_join_j8/overlay.yaml``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The PR watcher's state file the real heads and their check runs are read from.
    watcher_state_path: str = Field(min_length=1)
    #: A watcher state older than this is refused, never judged.
    watcher_max_age_seconds: int = Field(ge=1)
    #: The GitHub owner the watcher's ``repo#n`` keys belong to.
    owner: str = Field(min_length=1)
    #: The lab-bound check the landing decision waits for, and the repositories that carry it.
    lab_check_name: str = Field(min_length=1)
    lab_bound_repos: tuple[str, ...] = Field(min_length=1)
    #: How many candidate heads one run looks through for the wanted end of the join.
    max_candidates: int = Field(ge=1)
    #: Piece 8's emission identity: the node that reads the lab-bound check run posted on the head.
    poster_node: str = Field(min_length=1)
    #: The failure code piece 8 ends with when the lab-bound check did not pass on the current attempt.
    poster_failure_code: str = Field(min_length=1)
    #: The floor's emission journal both chains are written to (append-only JSON lines).
    emission_journal: str = Field(min_length=1)
    #: A drill run is tagged so no join counts it as evidence.
    drill: bool
    #: Whether this overlay may run a named injected break (the join's failing control).
    allow_injected_break: bool


class ModelJ8Subject(BaseModel):
    """One real pull request head as the collector read it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr: str = Field(pattern=PR_KEY_PATTERN)
    head_sha: str
    #: The facts the landing decision judges.
    facts: ModelLandingFacts
    #: The facts piece 8's poster judges; the same as ``facts`` unless a break makes them differ.
    poster_facts: ModelLandingFacts
    #: The verdict of the lab-bound check on this head, as the join will read it.
    verdict: EnumLabCheckVerdict


class ModelJoinJ8LiveCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    want: EnumJ8Want
    pr: str | None = Field(default=None, pattern=PR_KEY_PATTERN)
    inject: EnumJ8Break | None = None


class EnumJ8LiveStatus(StrEnum):
    COMPLETED = "completed"
    PENDING = "pending"
    NO_SUBJECT = "no_subject"
    FAILED = "failed"


class ModelJoinJ8LiveResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumJ8LiveStatus
    correlation_id: str
    pr: str | None = None
    head_sha: str | None = None
    verdict: EnumLabCheckVerdict | None = None
    outcome: EnumJoinJ8Outcome | None = None
    poster_ended: str | None = None
    injected: EnumJ8Break | None = None
    detail: str = ""


__all__ = [
    "OVERLAY_ID",
    "EnumJ8Break",
    "EnumJ8LiveStatus",
    "EnumJ8Want",
    "ModelJ8Subject",
    "ModelJoinJ8LiveCommand",
    "ModelJoinJ8LiveOverlay",
    "ModelJoinJ8LiveResult",
]
