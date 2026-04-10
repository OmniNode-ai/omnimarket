# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Handler integration dry-run tests for node_onboarding (OMN-8277).

These tests instantiate HandlerOnboarding directly and inspect the returned
dict. They prove policy resolution and handler behavior in isolation — no
event bus or Kafka required. dry_run=True ensures verifications are not
executed (keeping tests fast and infra-free).
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_onboarding.handlers.handler_onboarding import (
    HandlerOnboarding,
)
from omnimarket.nodes.node_onboarding.models.model_onboarding_start_command import (
    ModelOnboardingStartCommand,
)


class TestGoldenChainOnboarding:
    """Handler integration tests for node_onboarding."""

    def test_dry_run_standalone_quickstart(self) -> None:
        """Dry run resolves correct steps without executing verifications."""
        handler = HandlerOnboarding()
        cmd = ModelOnboardingStartCommand(
            policy_name="standalone_quickstart",
            dry_run=True,
        )
        result = handler.handle(cmd)
        assert result["dry_run"] is True
        assert result["total_steps"] > 0
        # standalone_quickstart targets first_node_running
        # which requires: check_python -> install_uv -> install_core -> create_first_node -> run_standalone_node
        assert "check_python" in result["resolved_steps"]
        assert "run_standalone_node" in result["resolved_steps"]

    def test_dry_run_new_employee(self) -> None:
        """new_employee policy resolves all expected steps."""
        handler = HandlerOnboarding()
        cmd = ModelOnboardingStartCommand(
            policy_name="new_employee",
            dry_run=True,
        )
        result = handler.handle(cmd)
        assert result["dry_run"] is True
        assert result["total_steps"] >= 5
        assert "check_python" in result["resolved_steps"]
        # new_employee targets full platform including omnidash + secrets + event_bus + first_node
        assert result["total_steps"] >= 8

    def test_unknown_policy_raises(self) -> None:
        """Unknown policy name raises ValueError."""
        handler = HandlerOnboarding()
        cmd = ModelOnboardingStartCommand(policy_name="does_not_exist", dry_run=True)
        with pytest.raises(ValueError, match="Unknown policy"):
            handler.handle(cmd)

    def test_explicit_capabilities_bypass_policy(self) -> None:
        """Providing target_capabilities directly bypasses policy lookup."""
        handler = HandlerOnboarding()
        cmd = ModelOnboardingStartCommand(
            target_capabilities=["python_installed"],
            dry_run=True,
        )
        result = handler.handle(cmd)
        assert result["dry_run"] is True
        # check_python produces python_installed
        assert "check_python" in result["resolved_steps"]

    def test_dry_run_returns_rendered_output(self) -> None:
        """Dry run returns a non-empty rendered_output string."""
        handler = HandlerOnboarding()
        cmd = ModelOnboardingStartCommand(
            policy_name="standalone_quickstart",
            dry_run=True,
        )
        result = handler.handle(cmd)
        assert isinstance(result["rendered_output"], str)
        assert len(result["rendered_output"]) > 0

    def test_skip_steps_reduces_step_count(self) -> None:
        """skip_steps reduces the resolved step list."""
        handler = HandlerOnboarding()
        cmd_full = ModelOnboardingStartCommand(
            policy_name="standalone_quickstart",
            dry_run=True,
        )
        cmd_skipped = ModelOnboardingStartCommand(
            policy_name="standalone_quickstart",
            skip_steps=["check_python"],
            dry_run=True,
        )
        full_result = handler.handle(cmd_full)
        skipped_result = handler.handle(cmd_skipped)
        assert "check_python" not in skipped_result["resolved_steps"]
        assert skipped_result["total_steps"] < full_result["total_steps"]
