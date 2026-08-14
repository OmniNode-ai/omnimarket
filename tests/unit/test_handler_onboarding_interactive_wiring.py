# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Behavioural tests for the interactive onboarding wiring (OMN-16039).

Before this ticket the omnimarket handler built ``ModelOnboardingInput``
without ``policy_name`` and passed no ``input_adapter``, so the upstream
interactive branch (``_handle_interactive`` -> ``InteractiveExecutor`` ->
``ConfigWriter``) was unreachable from the user entry point.

These tests drive the real upstream ``handle_onboarding`` (no mock of the
routing decision) so they fail loudly if the wiring regresses.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from omnibase_infra.onboarding.adapter_fake_input import AdapterFakeInput

from omnimarket.nodes.node_onboarding.handlers.handler_onboarding import (
    HandlerOnboarding,
)
from omnimarket.nodes.node_onboarding.models.model_onboarding_start_command import (
    ModelOnboardingStartCommand,
)

_HANDLER_MODULE = "omnimarket.nodes.node_onboarding.handlers.handler_onboarding"

# Drives the interactive_onboarding policy down the "local, no llm_inference"
# branch: choose_deployment_mode -> configure_local_services -> write_config_local.
_LOCAL_PATH_RESPONSES: dict[str, str | list[str]] = {
    "choose_deployment_mode": "local",
    "configure_local_services": ["kafka", "postgres"],
}


@pytest.mark.unit
class TestInteractivePolicyIsReachable:
    """The interactive policy must actually reach the interactive executor."""

    async def test_interactive_policy_runs_interactive_path(self) -> None:
        """policy_name='interactive_onboarding' drives _handle_interactive."""
        with patch(
            f"{_HANDLER_MODULE}.AdapterCliInput",
            lambda: AdapterFakeInput(_LOCAL_PATH_RESPONSES),
        ):
            result = await HandlerOnboarding().handle(
                ModelOnboardingStartCommand(
                    policy_name="interactive_onboarding",
                    dry_run=True,
                )
            )

        assert result["policy_type"] == "interactive"
        assert result["policy_name"] == "interactive_onboarding"
        assert result["terminal_step"] == "write_config_local"
        assert result["visited_steps"] == [
            "choose_deployment_mode",
            "configure_local_services",
        ]
        assert result["success"] is True
        assert "ONEX_DEPLOYMENT_MODE=local" in result["rendered_output"]

    async def test_interactive_policy_receives_an_input_adapter(self) -> None:
        """handle_onboarding is called with policy_name AND a non-None adapter.

        Upstream raises OnboardingHandlerError when policy_name is set and
        input_adapter is None, so both halves of the wiring are load-bearing.
        """
        captured: dict[str, Any] = {}

        async def _spy(input_model: Any, input_adapter: Any = None) -> Any:
            captured["policy_name"] = input_model.policy_name
            captured["adapter"] = input_adapter
            captured["env_output_path"] = input_model.env_output_path
            captured["overlay_output_path"] = input_model.overlay_output_path
            captured["dry_run"] = input_model.dry_run
            raise _SpyStopError

        with (
            patch(f"{_HANDLER_MODULE}.handle_onboarding", _spy),
            patch(
                f"{_HANDLER_MODULE}.AdapterCliInput",
                lambda: AdapterFakeInput(_LOCAL_PATH_RESPONSES),
            ),
            pytest.raises(_SpyStopError),
        ):
            await HandlerOnboarding().handle(
                ModelOnboardingStartCommand(
                    policy_name="interactive_onboarding",
                    dry_run=False,
                    env_output_path="/tmp/onex.env",  # local-path-ok: test fixture
                    overlay_output_path="/tmp/overlay.yaml",  # local-path-ok: test fixture
                )
            )

        assert captured["policy_name"] == "interactive_onboarding"
        assert captured["adapter"] is not None
        assert captured["env_output_path"] == "/tmp/onex.env"  # local-path-ok
        assert captured["overlay_output_path"] == "/tmp/overlay.yaml"  # local-path-ok
        assert captured["dry_run"] is False

    async def test_interactive_write_mode_writes_overlay_and_env(
        self, tmp_path: Path
    ) -> None:
        """dry_run=False writes both the overlay YAML and the legacy .env."""
        env_path = tmp_path / "onex.env"
        overlay_path = tmp_path / "overlay.yaml"

        with patch(
            f"{_HANDLER_MODULE}.AdapterCliInput",
            lambda: AdapterFakeInput(_LOCAL_PATH_RESPONSES),
        ):
            result = await HandlerOnboarding().handle(
                ModelOnboardingStartCommand(
                    policy_name="interactive_onboarding",
                    dry_run=False,
                    env_output_path=str(env_path),
                    overlay_output_path=str(overlay_path),
                )
            )

        assert result["env_output_path_written"] == str(env_path)
        assert result["overlay_output_path_written"] == str(overlay_path)
        assert env_path.is_file()
        assert overlay_path.is_file()
        assert "ONEX_DEPLOYMENT_MODE" in env_path.read_text(encoding="utf-8")


@pytest.mark.unit
class TestDagBehaviourIsPreserved:
    """Non-interactive policies must keep the exact pre-existing DAG behaviour."""

    async def test_dag_policy_passes_no_policy_name_and_no_adapter(self) -> None:
        """A DAG policy must leave policy_name None so upstream stays on the DAG.

        Upstream routes on ``policy_name is not None`` (not on policy_type),
        so leaking policy_name for a DAG policy would silently reroute every
        non-interactive onboarding run into the interactive executor.
        """
        captured: dict[str, Any] = {}

        async def _spy(input_model: Any, input_adapter: Any = None) -> Any:
            captured["policy_name"] = input_model.policy_name
            captured["adapter"] = input_adapter
            captured["target_capabilities"] = list(input_model.target_capabilities)
            captured["skip_steps"] = list(input_model.skip_steps)
            captured["continue_on_failure"] = input_model.continue_on_failure
            raise _SpyStopError

        with (
            patch(f"{_HANDLER_MODULE}.handle_onboarding", _spy),
            pytest.raises(_SpyStopError),
        ):
            await HandlerOnboarding().handle(
                ModelOnboardingStartCommand(
                    policy_name="standalone_quickstart",
                    skip_steps=["check_uv"],
                    continue_on_failure=True,
                    dry_run=False,
                )
            )

        assert captured["policy_name"] is None
        assert captured["adapter"] is None
        assert captured["target_capabilities"]
        assert captured["skip_steps"] == ["check_uv"]
        assert captured["continue_on_failure"] is True

    async def test_dag_dry_run_still_returns_a_resolved_plan(self) -> None:
        """dry_run on a DAG policy keeps the verify-only plan response."""
        mock_handle = AsyncMock()
        with patch(f"{_HANDLER_MODULE}.handle_onboarding", mock_handle):
            result = await HandlerOnboarding().handle(
                ModelOnboardingStartCommand(
                    policy_name="standalone_quickstart",
                    dry_run=True,
                )
            )

        mock_handle.assert_not_awaited()
        assert result["dry_run"] is True
        assert result["execution_mode"] == "verify-only"
        assert result["resolved_steps"]

    async def test_unknown_policy_still_raises_value_error(self) -> None:
        """An unknown policy name with no explicit capabilities still fails fast."""
        with pytest.raises(ValueError, match="Unknown policy"):
            await HandlerOnboarding().handle(
                ModelOnboardingStartCommand(policy_name="no_such_policy")
            )


@pytest.mark.unit
class TestPolicyNameAbsent:
    """Callers that never name a policy must keep working."""

    async def test_explicit_capabilities_without_a_known_policy(self) -> None:
        """Explicit target_capabilities bypass policy lookup, as before."""
        captured: dict[str, Any] = {}

        async def _spy(input_model: Any, input_adapter: Any = None) -> Any:
            captured["policy_name"] = input_model.policy_name
            captured["adapter"] = input_adapter
            captured["target_capabilities"] = list(input_model.target_capabilities)
            raise _SpyStopError

        with (
            patch(f"{_HANDLER_MODULE}.handle_onboarding", _spy),
            pytest.raises(_SpyStopError),
        ):
            await HandlerOnboarding().handle(
                ModelOnboardingStartCommand(
                    policy_name="not_a_registered_policy",
                    target_capabilities=["python_installed"],
                    dry_run=False,
                )
            )

        assert captured["policy_name"] is None
        assert captured["adapter"] is None
        assert captured["target_capabilities"] == ["python_installed"]

    async def test_default_command_uses_the_setup_dag_policy(self) -> None:
        """ModelOnboardingStartCommand() defaults still resolve the setup DAG."""
        result = await HandlerOnboarding().handle(
            ModelOnboardingStartCommand(dry_run=True)
        )
        assert result["dry_run"] is True
        assert result["resolved_steps"]

    def test_output_path_fields_exist_on_the_command(self) -> None:
        """The command model carries the interactive output paths."""
        fields = ModelOnboardingStartCommand.model_fields
        assert "env_output_path" in fields
        assert "overlay_output_path" in fields
        cmd = ModelOnboardingStartCommand()
        assert cmd.env_output_path is None
        assert cmd.overlay_output_path is None


@pytest.mark.unit
class TestContractDeclaresTheInteractiveSurface:
    """contract.yaml must declare what the handler now accepts and emits."""

    CONTRACT = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_onboarding"
        / "contract.yaml"
    )

    def _contract(self) -> dict[str, Any]:
        data: dict[str, Any] = yaml.safe_load(self.CONTRACT.read_text(encoding="utf-8"))
        return data

    def test_terminal_event_is_declared_and_published(self) -> None:
        """The node's only terminal state is the onboarding-completed event."""
        contract = self._contract()
        assert contract["terminal_event"] == (
            "onex.evt.omnimarket.onboarding-completed.v1"
        )
        assert contract["event_bus"]["publish_topics"] == [
            "onex.evt.omnimarket.onboarding-completed.v1"
        ]
        assert contract["event_bus"]["subscribe_topics"] == [
            "onex.cmd.omnimarket.onboarding-start.v1"
        ]

    def test_declared_inputs_match_the_command_model(self) -> None:
        """Every command field is declared as a contract input, and vice versa."""
        declared = set(self._contract()["inputs"])
        assert declared == set(ModelOnboardingStartCommand.model_fields)

    def test_interactive_outputs_are_declared(self) -> None:
        """The interactive path's result keys are declared contract outputs."""
        declared = set(self._contract()["outputs"])
        for key in (
            "policy_type",
            "visited_steps",
            "terminal_step",
            "env_output_path_written",
            "overlay_output_path_written",
            "rendered_output",
        ):
            assert key in declared, f"contract.yaml does not declare output {key!r}"


class _SpyStopError(Exception):
    """Sentinel raised by spies to stop execution after capturing arguments."""
