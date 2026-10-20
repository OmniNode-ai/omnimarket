# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing controller's stall transitions, as the liveness seam reads them.

The controller holds a red cause, a red base branch or an open plugin version
bump and says so in its decision text every tick; until this event nothing on
the bus carried it, so a hold that never moved opened no episode (OMN-17427).
One event is emitted per state change of a subject: dispatched, held,
claim-refused, attempt-never-ran, released, merged. The automation-liveness
watchdog reads them as trigger expectations of a ``bus-event`` overlay entry:
a stalling transition opens the expectation and a moving one answers it.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict

from omnimarket.models.liveness.model_automation_liveness import NonEmptyStr


class EnumLandingStallSubject(StrEnum):
    """What the controller is holding."""

    #: A red cause (a check failing on a group of PRs).
    CAUSE = "cause"
    #: A red base branch: every PR on it waits.
    BASE_BRANCH = "base_branch"
    #: A plugin version bump pull request.
    VERSION_BUMP = "version_bump"


class EnumLandingStallTransition(StrEnum):
    """One state change of a held subject, in the controller's words."""

    DISPATCHED = "dispatched"
    HELD = "held"
    CLAIM_REFUSED = "claim_refused"
    ATTEMPT_NEVER_RAN = "attempt_never_ran"
    RELEASED = "released"
    MERGED = "merged"


#: Transitions that end a stall of each subject. A dispatched fix worker moves a
#: cause or a base; a version bump moves only when it merges or is released.
ANSWERING_TRANSITIONS: dict[
    EnumLandingStallSubject, frozenset[EnumLandingStallTransition]
] = {
    EnumLandingStallSubject.CAUSE: frozenset(
        {
            EnumLandingStallTransition.DISPATCHED,
            EnumLandingStallTransition.RELEASED,
            EnumLandingStallTransition.MERGED,
        }
    ),
    EnumLandingStallSubject.BASE_BRANCH: frozenset(
        {
            EnumLandingStallTransition.DISPATCHED,
            EnumLandingStallTransition.RELEASED,
            EnumLandingStallTransition.MERGED,
        }
    ),
    EnumLandingStallSubject.VERSION_BUMP: frozenset(
        {EnumLandingStallTransition.RELEASED, EnumLandingStallTransition.MERGED}
    ),
}


class ModelLandingStallTransition(BaseModel):
    """One transition of one held subject, published by the landing controller."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: EnumLandingStallSubject
    transition: EnumLandingStallTransition
    #: ``owner/repo``.
    repo: NonEmptyStr
    #: The cause key, the base branch name or the bump PR (``repo#n``).
    subject_key: NonEmptyStr
    #: The controller's decision text for the subject on this tick, verbatim.
    decision_text: NonEmptyStr
    observed_at: AwareDatetime
    #: The controller tick that decided it, when the controller numbers ticks.
    tick: int | None = None

    @property
    def match_key(self) -> str:
        """The correlation key: one expectation per repository and subject."""
        return f"{self.repo}:{self.subject.value}:{self.subject_key}"

    @property
    def answers(self) -> bool:
        return self.transition in ANSWERING_TRANSITIONS[self.subject]


__all__ = [
    "ANSWERING_TRANSITIONS",
    "EnumLandingStallSubject",
    "EnumLandingStallTransition",
    "ModelLandingStallTransition",
]
