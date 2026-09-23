# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models for the per-attempt continuous-integration outcome projection.

OMN-18903, decision 2 of epic OMN-18850.

The row's grain is (repository, pull request, head commit, check, run attempt),
which is the finest grain the code host reports an outcome at and the coarsest
one at which the eval metric is still answerable. The reasoning for each key
column is in the create migration and is not repeated here.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.merge_control.reason_code_classifier import (
    EnumCiAttemptCauseClass,
    EnumMergeCheckReasonCode,
    MergeCheckVerdict,
    eval_cause_class,
)

#: A ticket identifier as it appears in a pull-request title. Anchored to the
#: token boundary so a longer identifier is not silently truncated.
_TICKET_PATTERN = re.compile(r"\bOMN-(\d+)\b", re.IGNORECASE)


def parse_ticket_id(title: str) -> str | None:
    """Return the ticket identifier in a pull-request title, or ``None``.

    The FIRST match wins, which matters: a title naming a second ticket in
    passing must not change the work unit a row is attributed to. Returning
    ``None`` is a real answer -- 2 of the 307 pull requests measured over the
    seven days to 2026-09-20 carry no ticket -- and the caller records it as
    null and counts it rather than guessing.
    """
    match = _TICKET_PATTERN.search(title or "")
    if match is None:
        return None
    return f"OMN-{match.group(1)}"


class ModelCiAttemptOutcomeRow(BaseModel):
    """One (check, attempt) outcome, ready to write."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(..., min_length=1)
    pr_number: int = Field(..., ge=1)
    head_sha: str = Field(..., pattern=r"^[0-9a-f]{40}$")
    check_name: str = Field(..., min_length=1)
    run_attempt: int = Field(..., ge=1)

    cause_code: EnumMergeCheckReasonCode
    cause_affirmative: bool
    attempt_ordinal: int = Field(..., ge=1)

    ticket_id: str | None = None
    failed_step_name: str | None = None
    run_id: str | None = None
    job_conclusion: str | None = None

    observed_at: datetime

    @property
    def cause_class(self) -> EnumCiAttemptCauseClass:
        """The four-way class the metric reports, derived from the verdict.

        Derived here rather than stored, because it is a VIEW over two columns
        that are stored. A stored class could disagree with its own code.
        """
        return eval_cause_class(
            MergeCheckVerdict(code=self.cause_code, affirmative=self.cause_affirmative)
        )

    @model_validator(mode="after")
    def _observed_at_is_timezone_aware(self) -> ModelCiAttemptOutcomeRow:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must carry a timezone")
        return self


class ModelCiAttemptCheck(BaseModel):
    """One classified check, as the inventory event carries it.

    Every attempt-identity field is OPTIONAL and defaults to ``None``. That is
    the consumer-first half of the OMN-18868 class: this model ships and is
    deployed BEFORE the producer emits the fields, so a payload that omits
    them must validate. A check that carries no attempt identity yields no
    row, which is correct -- a green check has no outcome to record and an
    unclassified one has no cause.
    """

    model_config = ConfigDict(extra="ignore")

    name: str = ""
    conclusion: str | None = None
    reason_code: EnumMergeCheckReasonCode | None = None
    # OMN-18904 supplies these. Until it does they are absent and this
    # projection writes nothing, which is the intended consumer-first state.
    head_sha: str | None = None
    run_attempt: int | None = None
    run_id: str | None = None
    failed_step_name: str | None = None
    cause_affirmative: bool | None = None


class ModelCiAttemptPullRequest(BaseModel):
    """One pull request's checks, plus the commit order the ordinal needs."""

    model_config = ConfigDict(extra="ignore")

    repo: str = ""
    pr_number: int = 0
    title: str = ""
    #: The pull request's head commits, OLDEST FIRST. The attempt ordinal is
    #: an index into this tuple. It is supplied by the producer rather than
    #: derived from arrival order, because arrival order is wrong for a
    #: redelivery, a backfill, or a consumer restarted mid-partition -- and
    #: wrong in a way that reads as data.
    head_sha_history: tuple[str, ...] = ()
    check_runs: tuple[ModelCiAttemptCheck, ...] = ()


class ModelCiAttemptOutcomeProjectionRequest(BaseModel):
    """The fold's input: one inventory-completed event, plus envelope time.

    Accepts the BARE event dict the runtime dispatches, with its own
    underscore-prefixed injections, rather than a payload nested under a key
    the producer has never sent. That mismatch is what made every
    runner-fleet message fail validation while its offsets committed
    (OMN-18880), and it is cheap to not repeat.
    """

    model_config = ConfigDict(extra="ignore")

    pull_requests: tuple[ModelCiAttemptPullRequest, ...] = ()
    #: Producer-assigned event time from the envelope. The fold takes it as
    #: INPUT and never reads a clock, which is what keeps it a pure function:
    #: the same event always derives the same rows.
    observed_at: datetime

    @model_validator(mode="before")
    @classmethod
    def _accept_the_bare_event_dict(cls, data: Any) -> Any:
        """Map the inventory event's own field names onto this request."""
        if not isinstance(data, dict):
            return data
        payload = dict(data)
        if "pull_requests" not in payload and "pr_states" in payload:
            payload["pull_requests"] = payload.get("pr_states") or ()
        if "observed_at" not in payload:
            envelope_timestamp = payload.get("_envelope_timestamp")
            if envelope_timestamp is not None:
                payload["observed_at"] = envelope_timestamp
        return payload


class ModelCiAttemptOutcomeProjectionResult(BaseModel):
    """The fold's output: the rows to write, and what could not be attributed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows: tuple[ModelCiAttemptOutcomeRow, ...] = ()
    #: Rows written with a null ticket because the pull-request title carried
    #: none. Counted rather than dropped: a dropped row makes the metric's
    #: denominator wrong with nothing to notice it.
    unattributed_row_count: int = 0
    #: Checks skipped because they carried no attempt identity -- a green
    #: check, or one the producer has not yet been taught to describe. Also
    #: counted, so "this projection wrote nothing" and "this projection was
    #: given nothing to write" stay distinguishable.
    skipped_check_count: int = 0


__all__ = [
    "ModelCiAttemptCheck",
    "ModelCiAttemptOutcomeProjectionRequest",
    "ModelCiAttemptOutcomeProjectionResult",
    "ModelCiAttemptOutcomeRow",
    "ModelCiAttemptPullRequest",
    "parse_ticket_id",
]
