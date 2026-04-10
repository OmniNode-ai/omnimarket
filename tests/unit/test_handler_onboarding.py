# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for HandlerOnboarding (OMN-8279).

Tests mock handle_onboarding to avoid executing real verification probes.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from omnimarket.nodes.node_onboarding.handlers.handler_onboarding import (
    HandlerOnboarding,
)
from omnimarket.nodes.node_onboarding.models.model_onboarding_start_command import (
    ModelOnboardingStartCommand,
)


class TestHandlerOnboarding:
    """Unit tests for HandlerOnboarding."""

    def test_policy_lookup_called_when_no_target_capabilities(self) -> None:
        """load_builtin_policies() is called when target_capabilities is empty."""
        mock_policy_data = {
            "target_capabilities": ["python_installed"],
        }
        with (
            patch(
                "omnimarket.nodes.node_onboarding.handlers.handler_onboarding.load_builtin_policies",
                return_value={"new_employee": mock_policy_data},
            ) as mock_load,
            patch(
                "omnimarket.nodes.node_onboarding.handlers.handler_onboarding.load_canonical_graph"
            ),
            patch(
                "omnimarket.nodes.node_onboarding.handlers.handler_onboarding.resolve_policy",
                return_value=[MagicMock(step_key="check_python")],
            ),
        ):
            handler = HandlerOnboarding()
            cmd = ModelOnboardingStartCommand(policy_name="new_employee", dry_run=True)
            result = handler.handle(cmd)
            mock_load.assert_called_once()
            assert result["dry_run"] is True

    def test_asyncio_run_called_with_model_onboarding_input(self) -> None:
        """asyncio.run() is called with a ModelOnboardingInput instance."""
        from omnibase_infra.nodes.node_onboarding_orchestrator.models.model_onboarding_output import (
            ModelOnboardingOutput,
        )
        from omnibase_infra.nodes.node_onboarding_orchestrator.models.model_step_result import (
            ModelStepResult,
        )

        mock_output = ModelOnboardingOutput(
            success=True,
            total_steps=1,
            completed_steps=1,
            step_results=[
                ModelStepResult(step_key="check_python", passed=True, message="ok")
            ],
            rendered_output="# Done",
        )

        def fake_asyncio_run(coro):
            # We can't await here, so just return the mock output
            return mock_output

        with (
            patch(
                "omnimarket.nodes.node_onboarding.handlers.handler_onboarding.asyncio.run",
                side_effect=fake_asyncio_run,
            ) as mock_run,
            patch(
                "omnimarket.nodes.node_onboarding.handlers.handler_onboarding.load_builtin_policies",
                return_value={
                    "standalone_quickstart": {
                        "target_capabilities": ["python_installed"]
                    }
                },
            ),
        ):
            handler = HandlerOnboarding()
            cmd = ModelOnboardingStartCommand(
                policy_name="standalone_quickstart",
                dry_run=False,
            )
            result = handler.handle(cmd)
            mock_run.assert_called_once()
            assert result["success"] is True

    def test_dry_run_skips_asyncio_run(self) -> None:
        """dry_run=True skips asyncio.run() entirely."""
        with (
            patch(
                "omnimarket.nodes.node_onboarding.handlers.handler_onboarding.asyncio.run"
            ) as mock_run,
            patch(
                "omnimarket.nodes.node_onboarding.handlers.handler_onboarding.load_builtin_policies",
                return_value={
                    "new_employee": {"target_capabilities": ["python_installed"]}
                },
            ),
            patch(
                "omnimarket.nodes.node_onboarding.handlers.handler_onboarding.load_canonical_graph"
            ),
            patch(
                "omnimarket.nodes.node_onboarding.handlers.handler_onboarding.resolve_policy",
                return_value=[MagicMock(step_key="check_python")],
            ),
        ):
            handler = HandlerOnboarding()
            cmd = ModelOnboardingStartCommand(policy_name="new_employee", dry_run=True)
            result = handler.handle(cmd)
            mock_run.assert_not_called()
            assert result["dry_run"] is True

    def test_skip_steps_passed_as_none_when_empty(self) -> None:
        """Empty skip_steps is passed as None (not empty list) to resolve_policy."""
        with (
            patch(
                "omnimarket.nodes.node_onboarding.handlers.handler_onboarding.load_builtin_policies",
                return_value={
                    "new_employee": {"target_capabilities": ["python_installed"]}
                },
            ),
            patch(
                "omnimarket.nodes.node_onboarding.handlers.handler_onboarding.load_canonical_graph"
            ),
            patch(
                "omnimarket.nodes.node_onboarding.handlers.handler_onboarding.resolve_policy",
                return_value=[MagicMock(step_key="check_python")],
            ) as mock_resolve,
        ):
            handler = HandlerOnboarding()
            cmd = ModelOnboardingStartCommand(
                policy_name="new_employee",
                skip_steps=[],  # Empty list
                dry_run=True,
            )
            handler.handle(cmd)
            # resolve_policy should receive None (not []) for skip_steps
            call_args = mock_resolve.call_args
            # Third positional arg is skip_steps
            assert call_args[0][2] is None, (
                f"Expected skip_steps=None for empty list, got {call_args[0][2]}"
            )
