# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The messages the landing orchestrator consumes, one model per topic (OMN-19829).

The runtime coerces each topic's payload into the route's ``event_model``, and
the ``state_io`` wiring reads the row key off the validated payload by the
contract-declared key name (``landing_key``). None of the wire payloads the
workflow consumes carries that field, so each route's model here is the wire
model unchanged plus a ``landing_key`` property derived from its repository and
PR number. Validation, field names and serialization stay the producer's.

``ModelPrLandingReconcileCommand`` is the one new wire model: the per-row
reconciliation prompt the tick fan-out publishes (revision 1 of plan 5.1,
section 6), which carries the key itself.

``ModelPrLandingObservedPrompt`` (OMN-20866) reads the PR watcher's
``pr-state-observed`` payload as a prompt. It keeps only the fields the prompt
needs and ignores the rest of the watcher's wire payload, whose envelope
fields change without this workflow.
"""

from __future__ import annotations

from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omnimarket.events.github import ModelPrMergedEvent
from omnimarket.events.pr_landing_companion import ModelPrLandingCompanionOutcome
from omnimarket.events.pr_landing_github.model_pr_landing_github_completed import (
    ModelPrLandingGithubCompleted,
)
from omnimarket.events.pr_landing_github.model_pr_landing_github_failed import (
    ModelPrLandingGithubFailed,
)
from omnimarket.events.pr_lifecycle_fix.model_fix_command import (
    ModelPrLifecycleFixCommand,
)
from omnimarket.events.pr_state import EnumPrState
from omnimarket.models.ci_red_triage import ci_red_repo_slug
from omnimarket.nodes.node_pr_landing_orchestrator.models.model_pr_landing_observation import (
    REPOSITORY_PATTERN,
    fill_landing_key,
    landing_key,
)

_OP_FIELD = "op"


class ModelPrLandingAutobindPrompt(ModelPrLifecycleFixCommand):
    """I1: the autobind command a product push publishes. A prompt, not a snapshot."""

    @property
    def landing_key(self) -> str:
        return landing_key(self.repo, self.pr_number)

    @property
    def is_companion_command(self) -> bool:
        """True when the payload named an ``op``: the workflow's own companion command.

        A push prompt from ``publish_occ_autobind_command.py`` never carries
        ``op``; the orchestrator's derive, regenerate and verify always do, and
        they travel on the same topic to the producer. The orchestrator must
        not observe its own commands (the T2 ingress parser refuses them too).
        """
        return _OP_FIELD in self.model_fields_set


class ModelPrLandingMergedIngress(ModelPrMergedEvent):
    """I2: ``onex.evt.github.pr-merged.v1`` from the Actions producers."""

    @property
    def landing_key(self) -> str:
        return landing_key(self.repo, self.pr_number)


class ModelPrLandingCompanionOutcomeIngress(ModelPrLandingCompanionOutcome):
    """The producer's typed answer to one companion command (T5, T10)."""

    @property
    def landing_key(self) -> str:
        return landing_key(self.repository, self.pr_number)


class ModelPrLandingGithubCompletedIngress(ModelPrLandingGithubCompleted):
    """The GitHub landing effect's completion (T4, T9)."""

    @property
    def landing_key(self) -> str:
        return landing_key(self.repository, self.pr_number)


class ModelPrLandingGithubFailedIngress(ModelPrLandingGithubFailed):
    """The GitHub landing effect's typed failure (T4, T9)."""

    @property
    def landing_key(self) -> str:
        return landing_key(self.repository, self.pr_number)


class ModelPrLandingReconcileCommand(BaseModel):
    """Reconcile one landing row: read the PR, apply any expired bound, drain the outbox.

    Published once per non-terminal row (PARKED included) on the tick's
    interval. The orchestrator answers with a conditional ``read_pr_state``,
    so an unchanged PR costs no quota (section 6).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(..., pattern=REPOSITORY_PATTERN)
    pr_number: int = Field(..., ge=1)
    landing_key: str = Field(..., description="``owner/repo#123``, the state_io key.")
    requested_at: datetime = Field(
        ..., description="The tick's time; the orchestrator reads no clock."
    )
    tick_id: str = Field(
        ..., min_length=1, description="The tick that produced this prompt, for dedup."
    )

    @model_validator(mode="before")
    @classmethod
    def _derive_landing_key(cls, data: object) -> object:
        return fill_landing_key(data)

    @field_validator("requested_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            msg = "requested_at must be timezone-aware"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _key_matches(self) -> Self:
        if self.landing_key != landing_key(self.repository, self.pr_number):
            msg = "landing_key disagrees with repository and pr_number"
            raise ValueError(msg)
        return self


class ModelPrLandingObservedPrompt(BaseModel):
    """I3: one PR watcher observation (``pr-state-observed``), read as a prompt.

    Like the push prompt it is not a snapshot: the orchestrator answers with a
    conditional ``read_pr_state`` and dispatches whatever the row has queued,
    so a PR whose checks were pending is read again on the watcher's cadence.
    Only the repositories the contract names (``observed_prompt_repositories``)
    are prompted; every other observation is dropped before any read.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    repo: str = Field(..., min_length=1, description="name or owner/name.")
    pr_number: int = Field(..., ge=1)
    state: EnumPrState
    head_sha: str = Field(..., description="The head the watcher observed.")
    observed_at: datetime = Field(..., description="When the watcher observed it.")

    @field_validator("observed_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            msg = "observed_at must be timezone-aware"
            raise ValueError(msg)
        return value

    @property
    def repository(self) -> str:
        return ci_red_repo_slug(self.repo)

    @property
    def landing_key(self) -> str:
        return landing_key(self.repository, self.pr_number)


PrLandingOrchestratorInput = (
    ModelPrLandingAutobindPrompt
    | ModelPrLandingObservedPrompt
    | ModelPrLandingMergedIngress
    | ModelPrLandingCompanionOutcomeIngress
    | ModelPrLandingGithubCompletedIngress
    | ModelPrLandingGithubFailedIngress
    | ModelPrLandingReconcileCommand
)


__all__: list[str] = [
    "ModelPrLandingAutobindPrompt",
    "ModelPrLandingCompanionOutcomeIngress",
    "ModelPrLandingGithubCompletedIngress",
    "ModelPrLandingGithubFailedIngress",
    "ModelPrLandingMergedIngress",
    "ModelPrLandingObservedPrompt",
    "ModelPrLandingReconcileCommand",
    "PrLandingOrchestratorInput",
]
