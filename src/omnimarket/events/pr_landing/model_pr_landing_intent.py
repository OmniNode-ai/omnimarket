# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A side effect one landing transition asks the orchestrator to issue."""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.events.pr_landing.enum_pr_landing_agent_reason import (
    EnumPrLandingAgentReason,
)
from omnimarket.events.pr_landing.enum_pr_landing_intent_kind import (
    EnumPrLandingIntentKind,
)
from omnimarket.events.pr_landing.model_pr_landing_observation import (
    HEAD_SHA_PATTERN,
    REPOSITORY_PATTERN,
)


class ModelPrLandingIntent(BaseModel):
    """One typed intent. The reducer returns these; it never performs them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumPrLandingIntentKind = Field(...)
    repository: str = Field(..., pattern=REPOSITORY_PATTERN)
    pr_number: int = Field(..., ge=1, description="The product PR.")
    head_sha: str | None = Field(
        default=None,
        pattern=HEAD_SHA_PATTERN,
        description="The head the intent is about, when the row knows it.",
    )
    target_pr: int | None = Field(
        default=None,
        ge=1,
        description=(
            "A different PR the intent acts on: the companion, for "
            "github.arm(occ_pr). None means the product PR itself."
        ),
    )
    check_runs: tuple[str, ...] = Field(
        default=(),
        description="The named runs a github.rerun re-runs. Empty for every other kind.",
    )
    agent_reason: EnumPrLandingAgentReason | None = Field(
        default=None, description="Set on agent_needed and on nothing else."
    )
    command_id: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "Set exactly on companion.derive and companion.regenerate: the id "
            "the row records as the command in flight (F4)."
        ),
    )
    detail: str | None = Field(
        default=None,
        min_length=1,
        description="Deterministic detail: a decline reason, a check list, a state name.",
    )

    @model_validator(mode="after")
    def _fields_match_the_kind(self) -> Self:
        is_agent = self.kind is EnumPrLandingIntentKind.AGENT_NEEDED
        if is_agent != (self.agent_reason is not None):
            msg = "agent_reason is set exactly on agent_needed intents"
            raise ValueError(msg)
        is_rerun = self.kind is EnumPrLandingIntentKind.GITHUB_RERUN
        if is_rerun != bool(self.check_runs):
            msg = "check_runs is non-empty exactly on github.rerun intents"
            raise ValueError(msg)
        if len(set(self.check_runs)) != len(self.check_runs):
            msg = "a github.rerun names each run once"
            raise ValueError(msg)
        tracked = self.kind in (
            EnumPrLandingIntentKind.COMPANION_DERIVE,
            EnumPrLandingIntentKind.COMPANION_REGENERATE,
        )
        if tracked != (self.command_id is not None):
            msg = (
                "command_id is set exactly on companion.derive and companion.regenerate"
            )
            raise ValueError(msg)
        arms_product = self.target_pr is None and self.kind in (
            EnumPrLandingIntentKind.GITHUB_ARM,
            EnumPrLandingIntentKind.GITHUB_ENQUEUE,
        )
        if arms_product and self.head_sha is None:
            # R4: the arm carries the head it was judged for, so GitHub refuses
            # it once the head has moved.
            msg = "an arm or enqueue of the product PR carries its expected head"
            raise ValueError(msg)
        return self


__all__: list[str] = ["ModelPrLandingIntent"]
