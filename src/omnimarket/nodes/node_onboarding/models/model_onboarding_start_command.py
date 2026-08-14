# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Input command model for node_onboarding (OMN-8273)."""

from pydantic import BaseModel, Field


class ModelOnboardingStartCommand(BaseModel):
    """Input command for node_onboarding.

    Accepts a policy name or explicit target capabilities.
    When both are provided, target_capabilities takes precedence.

    ``env_output_path`` / ``overlay_output_path`` are only consumed by the
    interactive path (a policy whose ``policy_type`` is ``"interactive"``).
    The DAG path ignores them.
    """

    policy_name: str = Field(default="setup")
    target_capabilities: list[str] = Field(default_factory=list)
    skip_steps: list[str] = Field(default_factory=list)
    continue_on_failure: bool = Field(default=False)
    dry_run: bool = Field(default=False)
    env_output_path: str | None = Field(
        default=None,
        description=(
            "Destination for the legacy .env write on the interactive path; "
            "required when dry_run=False"
        ),
    )
    overlay_output_path: str | None = Field(
        default=None,
        description=(
            "Destination for the overlay YAML write on the interactive path; "
            "derived from env_output_path when omitted"
        ),
    )


__all__ = ["ModelOnboardingStartCommand"]
