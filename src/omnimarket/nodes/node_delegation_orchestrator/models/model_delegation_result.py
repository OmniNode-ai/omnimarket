# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delegation terminals with answered-attempt provenance (OMN-19556)."""

from omnibase_core.models.delegation import wire as core_wire
from pydantic import Field

from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillResponseSourceAttempt,
)


class ModelDelegationResult(core_wire.ModelDelegationResult):
    """Core result contract extended with the source of retained content."""

    response_source_attempt: ModelDelegateSkillResponseSourceAttempt | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class ModelDelegationCompleted(
    core_wire.ModelDelegationCompleted, ModelDelegationResult
):
    """Completed result retaining Core's outcome invariants."""


class ModelDelegationFailed(core_wire.ModelDelegationFailed, ModelDelegationResult):
    """Failed result retaining Core's outcome invariants."""


__all__: list[str] = [
    "ModelDelegationCompleted",
    "ModelDelegationFailed",
    "ModelDelegationResult",
]
