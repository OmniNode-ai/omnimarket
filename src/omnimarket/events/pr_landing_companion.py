# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The companion seam of the PR landing workflow (OMN-19827, epic OMN-19822).

Two frozen names live here, owned by wave-1 task T5 of the PR landing workflow
plan (public knowledge-base repo, ``plans/2026-09-26-pr-landing-workflow-plan.md``
section 5.3):

* :class:`EnumPrLandingCompanionOp` -- the operation carried by the ``op`` field
  of the live occ-autobind command (``ModelPrLifecycleFixCommand``). Absent means
  ``derive``, so every publisher that predates the field keeps working unchanged.
* :class:`ModelPrLandingCompanionOutcome` -- the typed answer to one companion
  command, published on ``onex.evt.omnimarket.pr-landing-companion-outcome.v1``
  (:data:`omnimarket.events.topics.PR_LANDING_COMPANION_OUTCOME_TOPIC_V1`).

Since wave-2 task T10 (OMN-19832) the producer, ``node_pr_lifecycle_fix_effect``,
publishes this outcome for every autobind command it consumes and honours
``regenerate``. The outcome echoes the command's ``command_id``, which is how the
landing reducer correlates it with the command in flight (revision 1 of the
plan, F5): an outcome for any other ``command_id``, including ``None`` on an
ordinary push-driven command, is dropped. The reader in
``node_pr_lifecycle_fix_effect.handlers.occ_autobind_outcome_reader`` still maps
the check-run marker line the producer posts on every product PR onto this
model; that marker stays until wave 4.

The models are pure data: no I/O, no clock, no language model.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class EnumPrLandingCompanionOp(StrEnum):
    """What a companion command asks the producer to do.

    derive      mint the companion for the product PR's current head (today's
                behaviour, and the reading of a command that carries no ``op``).
    regenerate  re-mint an existing companion that conflicts with the change-control
                default branch, without waiting for a product push.
    verify      check that the companion is open, stamped on the product body and
                armed, and repair the stamp or the arm when it is not.
    """

    DERIVE = "derive"
    REGENERATE = "regenerate"
    VERIFY = "verify"


class EnumPrLandingCompanionOutcomeKind(StrEnum):
    """Terminal disposition of one companion command.

    The marker reader maps each check headline onto one kind. A ``NOOP``
    headline uses ``DECLINED`` with an ``ALREADY_BOUND`` or ``STAMP_REBOUND``
    code, preserving the bus contract and landing decisions (OMN-18939).
    """

    MINTED = "MINTED"
    DECLINED = "DECLINED"
    ERROR = "ERROR"


class EnumPrLandingCompanionDeclineCode(StrEnum):
    """Why a companion command minted nothing new.

    The ``skip:<CODE>`` codes are the live producer's own. The four without a
    ``skip:`` prefix classify the producer's other deliberate returns:

    AUTHORED_UNVERIFIED  the producer authored the companion and named it, but
                         its read-back verifier did not confirm it. Historic
                         DECLINED markers retain this code; new markers report
                         MINTED with an unknown stamp. The companion exists.
    ALREADY_BOUND        the product body already names a companion.
    STAMP_REBOUND        the product body was re-pointed at the proven companion.
    DRY_RUN              the producer ran without side effects.
    WINDOW_IN_FLIGHT     the member was not pushed to its repository's batch
                         window because the window's change-control run is in
                         flight or the window is armed and green; it binds on
                         the first rebuild after that run settles (OMN-20042).
    NOTHING_TO_BIND      the companion branch carried no commit past the OCC
                         default branch, so GitHub refused to open a pull
                         request for it ("No commits between"): what this
                         command would write is already on the default branch,
                         typically because the companion already merged.
    UNCLASSIFIED        a reason this seam does not recognise. It is typed and
                         visible, never dropped; a recorded line that lands here
                         fails the mapping's corpus test.
    """

    AUTHORED_UNVERIFIED = "AUTHORED_UNVERIFIED"
    ALREADY_BOUND = "ALREADY_BOUND"
    STAMP_REBOUND = "STAMP_REBOUND"
    DRY_RUN = "DRY_RUN"
    OCC_SELF_COMPANION = "OCC_SELF_COMPANION"
    PR_CLOSED = "PR_CLOSED"
    PR_DRAFT = "PR_DRAFT"
    PR_DO_NOT_MERGE = "PR_DO_NOT_MERGE"
    STAMP_REBIND_UNPROVEN = "STAMP_REBIND_UNPROVEN"
    DEFER_HAND_AUTHORED = "DEFER_HAND_AUTHORED"
    DEPENDENCY_PIN_ONLY = "DEPENDENCY_PIN_ONLY"
    NO_RED_DERIVABLE_CHECK = "NO_RED_DERIVABLE_CHECK"
    LEASE_HELD = "LEASE_HELD"
    TICKET_LEASE_HELD = "TICKET_LEASE_HELD"
    WINDOW_IN_FLIGHT = "WINDOW_IN_FLIGHT"
    NOTHING_TO_BIND = "NOTHING_TO_BIND"
    UNCLASSIFIED = "UNCLASSIFIED"


class ModelPrLandingCompanionOutcome(BaseModel):
    """The typed outcome of one companion command for one product PR head.

    ``stamped``, ``armed`` and ``conflicting`` are tri-state on purpose: ``None``
    means the surface that produced this outcome did not observe the fact, which
    is different from observing that it is false. The landing reducer asks for a
    ``verify`` when it needs a fact that is ``None``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumPrLandingCompanionOutcomeKind = Field(
        ..., description="MINTED, DECLINED or ERROR."
    )
    op: EnumPrLandingCompanionOp = Field(
        default=EnumPrLandingCompanionOp.DERIVE,
        description="The operation this outcome answers.",
    )
    repository: str = Field(..., description="Product repository, owner/name.")
    pr_number: int = Field(..., gt=0, description="Product PR number.")
    head_sha: str = Field(..., description="Product PR head the outcome is bound to.")
    correlation_id: UUID | None = Field(
        default=None, description="The command's correlation id, when it had one."
    )
    command_id: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "The command_id of the companion command this outcome answers, "
            "echoed from the command (F5). None when the command carried none, "
            "which is every push-driven command; the landing reducer drops an "
            "outcome whose command_id is not the one in flight."
        ),
    )
    occ_pr: int | None = Field(
        default=None,
        gt=0,
        description="The change-control companion PR, when the outcome names one.",
    )
    stamped: bool | None = Field(
        default=None,
        description="The product body names occ_pr as its evidence source.",
    )
    armed: bool | None = Field(
        default=None, description="Auto-merge is armed on the companion."
    )
    conflicting: bool | None = Field(
        default=None,
        description="The companion conflicts with the change-control default branch.",
    )
    decline_code: EnumPrLandingCompanionDeclineCode | None = Field(
        default=None, description="Set exactly when kind is DECLINED."
    )
    decline_reason: str | None = Field(
        default=None, description="The producer's reason, set exactly when DECLINED."
    )
    error_reason: str | None = Field(
        default=None, description="The producer's fault, set exactly when ERROR."
    )

    @field_validator("repository")
    @classmethod
    def _validate_repository(cls, value: str) -> str:
        if not _REPOSITORY_RE.fullmatch(value):
            raise ValueError(f"repository must be owner/name, got {value!r}")
        return value

    @field_validator("head_sha")
    @classmethod
    def _validate_head_sha(cls, value: str) -> str:
        if not _GIT_SHA_RE.fullmatch(value):
            raise ValueError("head_sha must be 7-40 hexadecimal characters")
        return value

    @model_validator(mode="after")
    def _fields_match_kind(self) -> Self:
        declined = self.kind is EnumPrLandingCompanionOutcomeKind.DECLINED
        errored = self.kind is EnumPrLandingCompanionOutcomeKind.ERROR
        if declined != (self.decline_code is not None):
            raise ValueError("decline_code is set exactly when kind is DECLINED")
        if declined != bool(self.decline_reason):
            raise ValueError("decline_reason is set exactly when kind is DECLINED")
        if errored != bool(self.error_reason):
            raise ValueError("error_reason is set exactly when kind is ERROR")
        if (
            self.kind is EnumPrLandingCompanionOutcomeKind.MINTED
            and self.occ_pr is None
        ):
            raise ValueError("a MINTED outcome names the companion it minted (occ_pr)")
        return self


__all__ = [
    "EnumPrLandingCompanionDeclineCode",
    "EnumPrLandingCompanionOp",
    "EnumPrLandingCompanionOutcomeKind",
    "ModelPrLandingCompanionOutcome",
]
