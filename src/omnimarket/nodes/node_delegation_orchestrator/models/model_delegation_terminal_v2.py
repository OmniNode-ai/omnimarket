# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Core v2 terminals extended with answered-attempt provenance (OMN-19556)."""

from omnibase_core.models.delegation.wire import model_delegation_terminal_v2 as core_v2
from omnibase_core.models.delegation.wire.model_delegation_terminal_v2 import (
    ModelDelegationProviderFailureCause,
    ModelDelegationQualityGateRejection,
    ModelQualityBarEvaluation,
)
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillResponseSourceAttempt,
)


class ModelDelegationTerminalV2(BaseModel):
    """Shared optional provenance on all concrete v2 outcomes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    response_source_attempt: ModelDelegateSkillResponseSourceAttempt | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class ModelDelegationTerminalCompletedV2(
    core_v2.ModelDelegationTerminalCompletedV2, ModelDelegationTerminalV2
):
    """Completed v2 result with Core's validators."""


class ModelDelegationTerminalFailedRoutedV2(
    core_v2.ModelDelegationTerminalFailedRoutedV2, ModelDelegationTerminalV2
):
    """Routed failure with Core's validators."""


class ModelDelegationTerminalFailedUnroutedV2(
    core_v2.ModelDelegationTerminalFailedUnroutedV2, ModelDelegationTerminalV2
):
    """Unrouted failure with Core's validators."""


__all__: list[str] = [
    "ModelDelegationProviderFailureCause",
    "ModelDelegationQualityGateRejection",
    "ModelDelegationTerminalCompletedV2",
    "ModelDelegationTerminalFailedRoutedV2",
    "ModelDelegationTerminalFailedUnroutedV2",
    "ModelDelegationTerminalV2",
    "ModelQualityBarEvaluation",
]
