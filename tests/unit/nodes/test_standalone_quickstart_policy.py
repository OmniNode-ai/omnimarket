# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""TDD tests for standalone_quickstart policy (OMN-8796).

Verifies that loading standalone_quickstart via HandlerOnboarding does not
raise ValueError, resolves 1-5 steps, and excludes infra-heavy steps tagged
docker/kafka/postgres/secrets.
"""

from __future__ import annotations

from omnimarket.nodes.node_onboarding.handlers.handler_onboarding import (
    HandlerOnboarding,
)
from omnimarket.nodes.node_onboarding.models.model_onboarding_start_command import (
    ModelOnboardingStartCommand,
)

_INFRA_STEPS = {
    "start_docker_infra",
    "start_event_bus",
    "connect_node_to_bus",
    "configure_secrets",
    "start_omnidash",
}


class TestStandaloneQuickstartPolicy:
    """Tests for the standalone_quickstart onboarding policy."""

    def test_loading_does_not_raise(self) -> None:
        """standalone_quickstart resolves without ValueError."""
        handler = HandlerOnboarding()
        cmd = ModelOnboardingStartCommand(
            policy_name="standalone_quickstart",
            dry_run=True,
        )
        result = handler.handle(cmd)
        assert result["dry_run"] is True

    def test_step_count_in_range(self) -> None:
        """Resolved plan contains between 1 and 5 steps inclusive."""
        handler = HandlerOnboarding()
        cmd = ModelOnboardingStartCommand(
            policy_name="standalone_quickstart",
            dry_run=True,
        )
        result = handler.handle(cmd)
        steps = result["resolved_steps"]
        assert 1 <= len(steps) <= 5, f"Expected 1-5 steps, got {len(steps)}: {steps}"

    def test_no_infra_steps_included(self) -> None:
        """Resolved plan does not contain docker/kafka/postgres/secrets steps."""
        handler = HandlerOnboarding()
        cmd = ModelOnboardingStartCommand(
            policy_name="standalone_quickstart",
            dry_run=True,
        )
        result = handler.handle(cmd)
        steps = set(result["resolved_steps"])
        infra_found = steps & _INFRA_STEPS
        assert not infra_found, f"Infra steps must not appear: {infra_found}"

    def test_first_node_running_capability_achieved(self) -> None:
        """Resolved plan includes run_standalone_node (produces first_node_running)."""
        handler = HandlerOnboarding()
        cmd = ModelOnboardingStartCommand(
            policy_name="standalone_quickstart",
            dry_run=True,
        )
        result = handler.handle(cmd)
        assert "run_standalone_node" in result["resolved_steps"]
