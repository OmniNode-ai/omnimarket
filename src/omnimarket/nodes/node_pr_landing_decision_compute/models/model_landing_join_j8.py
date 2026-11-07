# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""J8 join (piece 8 to piece 9) request, result and policy (OMN-22919).

The policy is the ``join_j8`` section of the decision node's contract: the two piece numbers, the emitting node's name
and the typed failure codes. Nothing in the handler spells one of them.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    ModelLandingFacts,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_state import (
    PR_KEY_PATTERN,
)

#: The floor's join fold reads a failure that never started as a refusal only when its code carries this prefix.
UPSTREAM_FAILURE_PREFIX = "upstream_"


class EnumJoinJ8Outcome(StrEnum):
    MERGEABLE = "mergeable"
    HELD_MISSING = "held_missing"
    HELD_SUPERSEDED = "held_superseded"
    HELD_FAILED = "held_failed"
    HELD_PENDING = "held_pending"
    HELD_OTHER = "held_other"


class ModelJoinJ8Policy(BaseModel):
    """The contract's ``join_j8`` section: the two piece numbers, the emitting node and the typed failure codes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    join_id: str = Field(pattern=r"^J\d{1,2}$")
    from_piece: int = Field(ge=1, le=18)
    to_piece: int = Field(ge=1, le=18)
    emission_node: str = Field(min_length=1)
    missing_failure_code: str = Field(min_length=1)
    superseded_failure_code: str = Field(min_length=1)
    failed_failure_code: str = Field(min_length=1)
    not_merged_failure_code: str = Field(min_length=1)

    @field_validator(
        "missing_failure_code", "superseded_failure_code", "failed_failure_code"
    )
    @classmethod
    def _upstream_shape(cls, value: str) -> str:
        if not value.startswith(UPSTREAM_FAILURE_PREFIX):
            raise ValueError(
                f"a refusal code must start with {UPSTREAM_FAILURE_PREFIX!r}: the floor reads a failure that never "
                "started as a join refusal only then"
            )
        return value


class ModelJoinJ8Request(BaseModel):
    """One PR of a tick's facts, judged under the correlation id piece 8's chain carries."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    pr: str = Field(pattern=PR_KEY_PATTERN)
    facts: ModelLandingFacts


class ModelJoinJ8Result(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: EnumJoinJ8Outcome
    correlation_id: str
    failure_code: str | None = None
    detail: str = ""
    merge_head: str | None = None
    skip_reason: str | None = None


__all__ = [
    "UPSTREAM_FAILURE_PREFIX",
    "EnumJoinJ8Outcome",
    "ModelJoinJ8Policy",
    "ModelJoinJ8Request",
    "ModelJoinJ8Result",
]
