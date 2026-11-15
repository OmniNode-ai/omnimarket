# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Protocol for setup steps."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models.model_setup_observation import ModelSetupObservation
from ..models.model_setup_plan import ModelSetupPlan


class SetupStepRefusedError(ValueError):
    """A step's apply() raises it when it will not act (for example a workspace
    folder that already holds other files); the setup workflow shows its message
    as the step's fix. Any other exception is reported only as a generic failure,
    because its text may carry secrets (OMN-23413; drafted by onex delegate run
    84792769)."""

    def __init__(self, message: str) -> None:
        if not message.strip():
            raise ValueError("a refusal needs a message for the user")
        super().__init__(message.strip())


@runtime_checkable
class ProtocolSetupStep(Protocol):
    """Protocol that every setup step must implement."""

    @property
    def name(self) -> str: ...

    def observe(self, plan: ModelSetupPlan) -> ModelSetupObservation: ...

    def apply(self, plan: ModelSetupPlan) -> ModelSetupObservation | None: ...
