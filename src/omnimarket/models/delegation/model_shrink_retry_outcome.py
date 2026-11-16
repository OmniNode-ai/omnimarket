# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the shrink path hands back to the dispatch ladder (OMN-20172)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.delegation.model_shrink_retry import EnumShrinkRetryReason
from omnimarket.models.delegation.wire.model_attempt_configuration import (
    ModelAttemptConfiguration,
)
from omnimarket.models.model_delegation_split_recombine import (
    ModelDelegationFinding,
)


class EnumShrinkRetryResolution(StrEnum):
    """How a rubric-failed whole attempt was settled."""

    CONFIG_SWAP = "config_swap"
    ACCEPTED = "accepted"
    ESCALATE = "escalate"


class ModelShrinkUnitOutcome(BaseModel):
    """One unit attempt as the dispatch ran it on the same model and backend."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    answer: str
    passed: bool
    findings: tuple[ModelDelegationFinding, ...] = ()
    attempt: dict[str, object] = Field(default_factory=dict)


class ModelShrinkWholeCheck(BaseModel):
    """The class rubric's verdict on the recombined whole."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    attempt: dict[str, object] = Field(default_factory=dict)


class ModelShrinkRetryOutcome(BaseModel):
    """The settled rubric failure: a swap to run, an accepted whole, or an escalation.

    ``events`` are the attempt records the path produced, in order, each already
    carrying its attempt kind, split id and parent attempt id.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    resolution: EnumShrinkRetryResolution
    reason: EnumShrinkRetryReason
    configuration: ModelAttemptConfiguration | None = None
    answer: str | None = None
    findings: tuple[ModelDelegationFinding, ...] = ()
    events: tuple[dict[str, object], ...] = ()
    unit_attempts: int = Field(default=0, ge=0)
    split_depth: int = Field(default=0, ge=0)


__all__ = [
    "EnumShrinkRetryResolution",
    "ModelShrinkRetryOutcome",
    "ModelShrinkUnitOutcome",
    "ModelShrinkWholeCheck",
]
