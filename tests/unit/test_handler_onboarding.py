# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for HandlerOnboarding (OMN-8279, OMN-13714).

Tests mock handle_onboarding to avoid executing real verification probes.
handle() is async; asyncio_mode=auto handles awaiting.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from omnimarket.nodes.node_onboarding.handlers.handler_onboarding import (
    HandlerOnboarding,
)
from omnimarket.nodes.node_onboarding.models.model_onboarding_start_command import (
    ModelOnboardingStartCommand,
)


class TestHandlerOnboarding:
    """Unit tests for HandlerOnboarding."""

    def test_default_policy_is_setup(self) -> None:
        """ModelOnboardingStartCommand() with no args defaults to setup policy (OMN-11053)."""
        cmd = ModelOnboardingStartCommand()
        assert cmd.policy_name == "setup"

    async def test_policy_lookup_called_when_no_target_capabilities(self) -> None:
        """load_builtin_policies() is called when target_capabilities is empty."""
        mock_policy_data = {
            "target_capabilities": ["python_installed"],
        }
        mock_step = MagicMock(
            step_key="check_python", produces_capabilities=["python_installed"]
        )
        with (
            patch(
                "omnimarket.nodes.node_onboarding.handlers.handler_onboarding.load_builtin_policies",
                return_value={"new_employee": mock_policy_data},
            ) as mock_load,
            patch(
                "omnimarket.nodes.node_onboarding.handlers.handler_onboarding._load_local_policies",
                return_value={},
            ),
            patch(
                "omnimarket.nodes.node_onboarding.handlers.handler_onboarding.load_canonical_graph"
            ),
            patch(
                "omnimarket.nodes.node_onboarding.handlers.handler_onboarding.resolve_policy",
                return_value=[mock_step],
            ),
        ):
            handler = HandlerOnboarding()
            cmd = ModelOnboardingStartCommand(policy_name="new_employee", dry_run=True)
            result = await handler.handle(cmd)
            mock_load.assert_called_once()
            assert result["dry_run"] is True
            assert result["execution_mode"] == "verify-only"
            assert result["requested_capabilities"] == ["python_installed"]
            assert result["produced_capabilities_per_step"] == {
                "check_python": ["python_installed"]
            }

    async def test_handle_onboarding_awaited_with_model_onboarding_input(
        self,
    ) -> None:
        """handle_onboarding coroutine is awaited directly (not via asyncio.run).

        Reproduces OMN-13714: asyncio.run() inside an already-running event loop
        raised RuntimeError. Fix: make handle() async and await the coroutine.
        """
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

        mock_handle_onboarding = AsyncMock(return_value=mock_output)

        with (
            patch(
                "omnimarket.nodes.node_onboarding.handlers.handler_onboarding.handle_onboarding",
                mock_handle_onboarding,
            ),
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
            # This must NOT raise RuntimeError (OMN-13714: asyncio.run inside loop)
            result = await handler.handle(cmd)
            mock_handle_onboarding.assert_awaited_once()
            assert result["success"] is True

    async def test_dry_run_does_not_call_handle_onboarding(self) -> None:
        """dry_run=True skips calling handle_onboarding entirely."""
        mock_handle_onboarding = AsyncMock()

        with (
            patch(
                "omnimarket.nodes.node_onboarding.handlers.handler_onboarding.handle_onboarding",
                mock_handle_onboarding,
            ),
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
            result = await handler.handle(cmd)
            mock_handle_onboarding.assert_not_awaited()
            assert result["dry_run"] is True

    async def test_skip_steps_passed_as_none_when_empty(self) -> None:
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
            await handler.handle(cmd)
            # resolve_policy should receive None (not []) for skip_steps
            call_args = mock_resolve.call_args
            # Third positional arg is skip_steps
            assert call_args[0][2] is None, (
                f"Expected skip_steps=None for empty list, got {call_args[0][2]}"
            )
