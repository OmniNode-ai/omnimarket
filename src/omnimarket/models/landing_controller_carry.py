# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the old landing controller decided one tick from and did for it, carried on the tick record (OMN-23665).

During the cutover period the old landing controller keeps landing while the composed workflow runs
beside it in observe mode. The workflow's own facts (the PR watcher's state) carry none of the
controller's state: no leases, records, worker results or blocker references, no gate it holds a
pull request behind. The landing snapshot step reads the controller's facts file for its last
finished tick and its decision log, and publishes both on the tick record as this model. It lives in
the shared models package because the snapshot step writes it and the tick orchestrator reads it:
the seam join test (tests/nodes/node_pr_landing_snapshot_effect/test_join_landing_snapshot_tick.py)
fails when either side changes its shape.

The read (``ModelLandingControllerCarry``) never travels: it holds the controller's whole facts, about 3 MB, and
a record carrying it was about 5.7 MB against a broker limit of 1,048,588 bytes, so every publish failed. What the
record carries is ``ModelLandingControllerDecisions``: the controller's decision per pull request for the tick,
read from its decision log (kind, subject, outcome, reason), and nothing of its facts. The comparison needs only
those rows; the facts' state the node decides from is carried onto the record's own facts, bounded by the
snapshot step against the overlay's record limit.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.landing_decision import (
    EnumLandingActionKind,
    ModelLandingExternalJudged,
    ModelLandingFacts,
)

REASON_MAX_LENGTH = 200
MAX_DECISION_ROWS = 2000


class ModelLandingControllerLogged(BaseModel):
    """One action row of the controller's decision log for the tick."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumLandingActionKind
    subject: str = Field(..., min_length=1)
    outcome: str = Field(..., min_length=1)
    reason: str = Field(
        default="",
        max_length=REASON_MAX_LENGTH,
        description="The log row's own reason or detail, cut to the bound; empty when the row names none.",
    )


class ModelLandingControllerJudged(BaseModel):
    """One external blocker the controller's sidecar holds as judged for a pull request (OMN-22921)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr: str = Field(
        ...,
        min_length=1,
        description="The pull request, repo#n as the controller keys it.",
    )
    judged: ModelLandingExternalJudged


class ModelLandingControllerCarry(BaseModel):
    """The controller's facts for one finished tick, and the actions its log shows it took."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    controller_tick: int = Field(
        ..., ge=1, description="The controller's own tick number."
    )
    observed_at: datetime = Field(
        description="The moment the controller's facts describe."
    )
    facts: ModelLandingFacts = Field(
        description="The facts the controller decided that tick from, as its facts file holds them."
    )
    logged: tuple[ModelLandingControllerLogged, ...] = Field(
        default=(),
        description="The action rows of the controller's decision log for that tick.",
    )
    judged: tuple[ModelLandingControllerJudged, ...] = Field(
        default=(),
        description=(
            "The external blockers the controller's sidecar holds as judged per pull request; "
            "the snapshot step fills them onto the matching pull requests' facts, and they never "
            "travel on the record."
        ),
    )
    source: str = Field(
        ..., min_length=1, description="The declared source the carry was read from."
    )


class ModelLandingControllerDecisions(BaseModel):
    """The controller's decision per pull request for one finished tick: all a tick record carries of it.

    No facts: the rows are the controller's real decision (what its log shows it decided and did), which
    is all the comparison reads of the controller. Bounded: the rows of one tick are at most
    ``MAX_DECISION_ROWS`` and each reason is cut to ``REASON_MAX_LENGTH`` characters.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    controller_tick: int = Field(
        ..., ge=1, description="The controller's own tick number."
    )
    observed_at: datetime = Field(
        description="The moment the controller's facts for that tick describe."
    )
    decisions: tuple[ModelLandingControllerLogged, ...] = Field(
        default=(),
        max_length=MAX_DECISION_ROWS,
        description="The controller's decision rows for that tick: kind, subject, outcome, reason.",
    )
    source: str = Field(
        ..., min_length=1, description="The declared source the rows were read from."
    )


def decisions_of(carry: ModelLandingControllerCarry) -> ModelLandingControllerDecisions:
    """The part of a controller read a tick record carries: its decision rows, never its facts."""
    return ModelLandingControllerDecisions(
        controller_tick=carry.controller_tick,
        observed_at=carry.observed_at,
        decisions=carry.logged,
        source=carry.source,
    )


__all__: list[str] = [
    "MAX_DECISION_ROWS",
    "REASON_MAX_LENGTH",
    "ModelLandingControllerCarry",
    "ModelLandingControllerDecisions",
    "ModelLandingControllerJudged",
    "ModelLandingControllerLogged",
    "decisions_of",
]
