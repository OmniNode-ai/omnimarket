# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One fact about one PR, normalized from whichever ingress reported it.

The landing workflow is keyed by ``(repository, pr_number)``. Every ingress
source (the autobind command a product push publishes, the pr-merged event the
Actions producers publish, and in wave 2 the companion outcome and the GitHub
landing effect results) is normalized into this one model before the reducer
sees it, so the reducer never parses a wire payload.

Two facts about the live ingress were measured on the dev-lane bus on
2026-09-26 and shape this model:

* Neither live payload carries a head sha. The autobind command's publisher
  deliberately leaves it off the wire (the effect re-resolves it), and the
  pr-merged event never had one. ``head_sha`` is therefore optional here, and a
  ``pushed`` observation without a sha means "a new head exists; read it".
* The autobind command is a prompt, not a snapshot (revision 1 of plan 5.1,
  section 6): it carries no head, no draft or hold flag and no ordering key.
  Its ``pushed`` observation has ``source_seq`` None, and the orchestrator
  answers it with a ``read_pr_state`` whose snapshot carries the key. Only a
  snapshot newer by ``source_seq`` moves a row (F1, F2).
* The autobind topic is also where the workflow's own companion commands go
  (the ``op`` field, owned by the companion seam). A payload that names an
  ``op`` is the workflow talking to the producer, not a push, and is refused
  as an ingress observation so the workflow cannot observe its own commands.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from omnimarket.events.github import ModelPrMergedEvent
from omnimarket.events.pr_head_check.enum_head_check_verdict import (
    HEAD_CHECK_RERUN_VERDICTS,
    EnumHeadCheckVerdict,
)
from omnimarket.events.pr_landing.enum_pr_landing_arm_method import (
    EnumPrLandingArmMethod,
)
from omnimarket.events.pr_landing.enum_pr_landing_companion_outcome import (
    EnumPrLandingCompanionOutcome,
)
from omnimarket.events.pr_landing.enum_pr_landing_observation_kind import (
    EnumPrLandingObservationKind,
)
from omnimarket.events.pr_landing.model_pr_landing_check_attempt import (
    ModelPrLandingCheckAttempt,
    unique_checks,
)
from omnimarket.events.pr_lifecycle_fix.model_fix_command import (
    EnumPrBlockReason,
    ModelPrLifecycleFixCommand,
)
from omnimarket.events.topics import OCC_AUTOBIND_COMMAND_TOPIC_V1, PR_MERGED_TOPIC_V1

REPOSITORY_PATTERN = r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
HEAD_SHA_PATTERN = r"^[0-9a-f]{40}$"

_OP_FIELD = "op"

_K = EnumPrLandingObservationKind

# Snapshots: what the orchestrator's read of the PR reports (section 6 of the
# revision). Only these carry the ordering key and the draft and held flags.
SNAPSHOT_KINDS: frozenset[EnumPrLandingObservationKind] = frozenset(
    {
        _K.PUSHED,
        _K.READY_FOR_REVIEW,
        _K.CONVERTED_TO_DRAFT,
        _K.TITLE_EDITED,
        _K.HOLD_APPLIED,
        _K.HOLD_LIFTED,
        _K.REOPENED,
        _K.CLOSED,
    }
)

# The draft and held values a snapshot kind states by itself.
_IMPLIED_DRAFT = {_K.CONVERTED_TO_DRAFT: True, _K.READY_FOR_REVIEW: False}
_IMPLIED_HELD = {_K.HOLD_APPLIED: True, _K.HOLD_LIFTED: False}

# Companion facts about the companion PR itself; each names it.
_COMPANION_PR_KINDS = frozenset(
    {_K.COMPANION_MERGED, _K.COMPANION_CONFLICTING, _K.COMPANION_CLOSED}
)


def landing_key(repository: str, pr_number: int) -> str:
    """The one string key of a landing row: ``owner/repo#123``."""
    return f"{repository}#{pr_number}"


def fill_landing_key(data: object) -> object:
    """Derive ``landing_key`` from repository and pr_number, or verify it.

    A stored row or a wire payload carries the key it was written with; one
    that disagrees with its own repository and PR number is refused rather
    than silently re-keyed.
    """
    if not isinstance(data, Mapping):
        return data
    repository = data.get("repository")
    pr_number = data.get("pr_number")
    if not isinstance(repository, str) or not isinstance(pr_number, int):
        return data
    derived = landing_key(repository, pr_number)
    given = data.get("landing_key")
    if given is None:
        return {**data, "landing_key": derived}
    if given != derived:
        msg = f"landing_key {given!r} does not match {derived!r}"
        raise ValueError(msg)
    return data


class ModelPrLandingObservation(BaseModel):
    """A normalized, typed fact the landing reducer can act on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(
        ...,
        pattern=REPOSITORY_PATTERN,
        description="Repository slug, owner/name.",
    )
    pr_number: int = Field(..., ge=1, description="Pull request number.")
    head_sha: str | None = Field(
        default=None,
        pattern=HEAD_SHA_PATTERN,
        description=(
            "Head commit the fact is about. None when the ingress does not "
            "carry one; merged and closed never need one."
        ),
    )
    kind: EnumPrLandingObservationKind = Field(
        ..., description="What happened. An unknown kind is refused."
    )
    observed_at: datetime = Field(
        ...,
        description=(
            "When the fact was true, taken from the source payload. The reducer "
            "reads time from here and never from a clock."
        ),
    )
    source_topic: str = Field(
        ..., min_length=1, description="Bus topic the fact arrived on."
    )
    source_event_id: str = Field(
        ...,
        min_length=1,
        description="The source's own id (correlation or event id), for dedup.",
    )
    ticket_ids: tuple[str, ...] = Field(
        default=(), description="Ticket ids the source named, in source order."
    )
    source_seq: int | None = Field(
        default=None,
        ge=1,
        description=(
            "The per-PR ordering key: the orchestrator's read sequence for the "
            "PR (F1, F2). None on a raw ingress prompt, which is not applied "
            "until a read supplies the key."
        ),
    )
    command_id: str | None = Field(
        default=None,
        min_length=1,
        description="The companion command a companion outcome answers (F5).",
    )
    episode: int | None = Field(
        default=None,
        ge=0,
        description="On a completion-bound expiry: the episode the bound was set in (R2b).",
    )
    state_entry_generation: int | None = Field(
        default=None,
        ge=0,
        description="On a completion-bound expiry: the state entry the bound was set for (R2b).",
    )
    check_attempts: tuple[ModelPrLandingCheckAttempt, ...] = Field(
        default=(),
        description="On a head-check verdict: the run attempt of each result read (F7).",
    )
    draft: bool | None = Field(
        default=None,
        description=(
            "On a snapshot: whether the PR is draft, as read. None means the "
            "snapshot did not read it; a kind that states it (converted_to_draft, "
            "ready_for_review) must agree."
        ),
    )
    held: bool | None = Field(
        default=None,
        description=(
            "On a snapshot: whether the PR carries a hold, as read. None means "
            "the snapshot did not read it; hold_applied and hold_lifted must agree."
        ),
    )
    verdict: EnumHeadCheckVerdict | None = Field(
        default=None,
        description="Set exactly on head_checks: the classifier's verdict (T3).",
    )
    rerun_checks: tuple[str, ...] = Field(
        default=(),
        description=(
            "On a head_checks verdict whose remedy is a re-run: the named runs. "
            "Empty on every other verdict, as the classifier's own model rules."
        ),
    )
    arm_method: EnumPrLandingArmMethod | None = Field(
        default=None,
        description=(
            "On head_checks only: the arm gate's answer and the repo's live merge "
            "policy, resolved by the orchestrator. None means the gate withholds "
            "the arm."
        ),
    )
    companion_outcome: EnumPrLandingCompanionOutcome | None = Field(
        default=None,
        description="Set exactly on companion_outcome: MINTED, DECLINED or ERROR.",
    )
    occ_pr: int | None = Field(
        default=None,
        ge=1,
        description=(
            "The companion PR: required on a MINTED outcome, companion_merged, "
            "companion_conflicting and companion_closed."
        ),
    )
    companion_stamped: bool | None = Field(
        default=None,
        description="On a MINTED outcome: whether the product body carries the stamp.",
    )
    companion_armed: bool | None = Field(
        default=None,
        description="On a MINTED outcome: whether the companion is armed.",
    )
    detail: str | None = Field(
        default=None,
        min_length=1,
        description="On a DECLINED outcome: the producer's reason, verbatim.",
    )
    companion_required: bool | None = Field(
        default=None,
        description=(
            "Set exactly on evaluation: whether this PR's change needs a "
            "change-control companion."
        ),
    )
    base_served: bool | None = Field(
        default=None,
        description=(
            "Set exactly on evaluation: whether the workflow serves the PR's "
            "base branch. An unserved base parks the row."
        ),
    )
    landing_key: str = Field(
        ...,
        description=(
            "``owner/repo#123``, the workflow row key (``state_io.key``). "
            "Derived when absent; refused when it disagrees with the fields."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _derive_landing_key(cls, data: object) -> object:
        return fill_landing_key(data)

    @field_validator("observed_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            msg = "observed_at must be timezone-aware"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _head_bound_kinds_carry_a_head(self) -> Self:
        if self.kind is EnumPrLandingObservationKind.HEAD_CHECKS and not self.head_sha:
            msg = "a head_checks observation must name the head it read"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _correlated_kinds_carry_their_correlation(self) -> Self:
        kind = self.kind
        is_outcome = kind is EnumPrLandingObservationKind.COMPANION_OUTCOME
        if is_outcome != (self.command_id is not None):
            msg = "command_id is set exactly on a companion_outcome observation"
            raise ValueError(msg)
        is_bound = kind is EnumPrLandingObservationKind.BOUND_EXPIRED
        tagged = self.episode is not None and self.state_entry_generation is not None
        untagged = self.episode is None and self.state_entry_generation is None
        if (is_bound and not tagged) or (not is_bound and not untagged):
            msg = (
                "episode and state_entry_generation are set exactly on a "
                "bound_expired observation"
            )
            raise ValueError(msg)
        if self.check_attempts and kind is not EnumPrLandingObservationKind.HEAD_CHECKS:
            msg = "check_attempts is set only on a head_checks observation"
            raise ValueError(msg)
        unique_checks(self.check_attempts)
        return self

    @model_validator(mode="after")
    def _snapshot_flags_agree_with_the_kind(self) -> Self:
        kind = self.kind
        is_snapshot = kind in SNAPSHOT_KINDS
        if not is_snapshot and (self.draft is not None or self.held is not None):
            msg = "draft and held are read only by a snapshot"
            raise ValueError(msg)
        implied_draft = _IMPLIED_DRAFT.get(kind)
        if implied_draft is not None and self.draft not in (None, implied_draft):
            msg = f"a {kind.value} snapshot cannot read draft={self.draft}"
            raise ValueError(msg)
        implied_held = _IMPLIED_HELD.get(kind)
        if implied_held is not None and self.held not in (None, implied_held):
            msg = f"a {kind.value} snapshot cannot read held={self.held}"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _verdict_fields_only_on_head_checks(self) -> Self:
        is_checks = self.kind is EnumPrLandingObservationKind.HEAD_CHECKS
        if is_checks != (self.verdict is not None):
            msg = "verdict is set exactly on a head_checks observation"
            raise ValueError(msg)
        if not is_checks and (self.rerun_checks or self.arm_method is not None):
            msg = "rerun_checks and arm_method are set only on head_checks"
            raise ValueError(msg)
        if any(not name.strip() for name in self.rerun_checks):
            msg = "rerun_checks holds a blank check name"
            raise ValueError(msg)
        if len(set(self.rerun_checks)) != len(self.rerun_checks):
            msg = "rerun_checks names each run once"
            raise ValueError(msg)
        reruns = self.verdict in HEAD_CHECK_RERUN_VERDICTS
        if is_checks and reruns != bool(self.rerun_checks):
            msg = "rerun_checks is non-empty exactly when the verdict's remedy is a re-run"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _companion_fields_match_the_kind(self) -> Self:
        kind = self.kind
        is_outcome = kind is EnumPrLandingObservationKind.COMPANION_OUTCOME
        if is_outcome != (self.companion_outcome is not None):
            msg = "companion_outcome is set exactly on a companion_outcome observation"
            raise ValueError(msg)
        minted = self.companion_outcome is EnumPrLandingCompanionOutcome.MINTED
        names_pr = minted or kind in _COMPANION_PR_KINDS
        if names_pr != (self.occ_pr is not None):
            msg = (
                "occ_pr is set exactly on a MINTED outcome, companion_merged, "
                "companion_conflicting and companion_closed"
            )
            raise ValueError(msg)
        minted_facts = {self.companion_stamped is None, self.companion_armed is None}
        if minted_facts != {not minted}:
            msg = "companion_stamped and companion_armed are set exactly on MINTED"
            raise ValueError(msg)
        declined = self.companion_outcome is EnumPrLandingCompanionOutcome.DECLINED
        if self.detail is not None and not declined:
            msg = "detail is set only on a DECLINED outcome"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _evaluation_carries_its_policy_facts(self) -> Self:
        is_eval = self.kind is EnumPrLandingObservationKind.EVALUATION
        facts = {self.companion_required is None, self.base_served is None}
        if facts != {not is_eval}:
            msg = "companion_required and base_served are set exactly on evaluation"
            raise ValueError(msg)
        return self

    @classmethod
    def from_ingress(cls, topic: str, payload: Mapping[str, object]) -> Self:
        """Normalize one recorded or live ingress payload.

        Raises ``ValueError`` for a topic this workflow does not ingest, and
        for a payload whose content does not map to exactly one known kind.
        """
        parser = _INGRESS_PARSERS.get(topic)
        if parser is None:
            msg = f"topic {topic!r} is not a landing ingress topic"
            raise ValueError(msg)
        fields = parser(payload)
        return cls.model_validate(fields)


def _from_occ_autobind(payload: Mapping[str, object]) -> dict[str, object]:
    if _OP_FIELD in payload:
        msg = (
            "an autobind command naming an op is the workflow's own companion "
            "command, not a push observation"
        )
        raise ValueError(msg)
    command = ModelPrLifecycleFixCommand.model_validate(dict(payload))
    if command.block_reason is not EnumPrBlockReason.RECEIPT_EVIDENCE_SOURCE_AUTOBIND:
        msg = (
            f"block_reason {command.block_reason.value!r} on the autobind topic "
            "maps to no landing observation kind"
        )
        raise ValueError(msg)
    return {
        "repository": command.repo,
        "pr_number": command.pr_number,
        "head_sha": None,
        "kind": EnumPrLandingObservationKind.PUSHED,
        "observed_at": command.requested_at,
        "source_topic": OCC_AUTOBIND_COMMAND_TOPIC_V1,
        "source_event_id": str(command.correlation_id),
        "ticket_ids": (command.ticket_id,) if command.ticket_id else (),
    }


def _from_pr_merged(payload: Mapping[str, object]) -> dict[str, object]:
    embedded_topic = payload.get("topic")
    if embedded_topic is not None and embedded_topic != PR_MERGED_TOPIC_V1:
        msg = f"pr-merged payload names topic {embedded_topic!r}"
        raise ValueError(msg)
    event = ModelPrMergedEvent.model_validate(dict(payload))
    if not re.fullmatch(REPOSITORY_PATTERN, event.repo):
        msg = f"pr-merged repo {event.repo!r} is not an owner/name slug"
        raise ValueError(msg)
    return {
        "repository": event.repo,
        "pr_number": event.pr_number,
        "head_sha": None,
        "kind": EnumPrLandingObservationKind.MERGED,
        "observed_at": datetime.fromisoformat(event.merged_at),
        "source_topic": PR_MERGED_TOPIC_V1,
        "source_event_id": event.event_id,
        "ticket_ids": (event.ticket,) if event.ticket else (),
    }


_INGRESS_PARSERS: Mapping[str, Callable[[Mapping[str, object]], dict[str, object]]] = {
    OCC_AUTOBIND_COMMAND_TOPIC_V1: _from_occ_autobind,
    PR_MERGED_TOPIC_V1: _from_pr_merged,
}

INGRESS_TOPICS: tuple[str, ...] = tuple(_INGRESS_PARSERS)


__all__: list[str] = [
    "HEAD_SHA_PATTERN",
    "INGRESS_TOPICS",
    "REPOSITORY_PATTERN",
    "SNAPSHOT_KINDS",
    "ModelPrLandingObservation",
    "fill_landing_key",
    "landing_key",
]
