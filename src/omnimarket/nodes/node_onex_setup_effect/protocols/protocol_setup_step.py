# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Protocol for setup steps."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models.model_setup_observation import ModelSetupObservation
from ..models.model_setup_plan import ModelSetupPlan


@runtime_checkable
class ProtocolSetupStep(Protocol):
    """Protocol that every setup step must implement."""

    @property
    def name(self) -> str: ...

    def observe(self, plan: ModelSetupPlan) -> ModelSetupObservation: ...

    def apply(self, plan: ModelSetupPlan) -> ModelSetupObservation | None: ...
