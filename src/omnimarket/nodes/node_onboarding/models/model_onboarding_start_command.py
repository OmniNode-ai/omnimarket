# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Input command model for node_onboarding (OMN-8273)."""

from pydantic import BaseModel, Field


class ModelOnboardingStartCommand(BaseModel):
    """Input command for node_onboarding.

    Accepts a policy name or explicit target capabilities.
    When both are provided, target_capabilities takes precedence.
    """

    policy_name: str = Field(default="new_employee")
    target_capabilities: list[str] = Field(default_factory=list)
    skip_steps: list[str] = Field(default_factory=list)
    continue_on_failure: bool = Field(default=False)
    dry_run: bool = Field(default=False)


__all__ = ["ModelOnboardingStartCommand"]
