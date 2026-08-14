# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Compute handler for node_onboarding (OMN-8273, OMN-16039).

Resolves a policy name to target_capabilities, constructs ModelOnboardingInput,
and delegates to handle_onboarding via await.

Two paths, chosen from the resolved policy's ``policy_type``:

- **interactive** — ``policy_name`` plus the output paths are forwarded and an
  ``AdapterCliInput`` is injected as ``input_adapter``, which is what routes
  upstream into ``_handle_interactive`` / ``InteractiveExecutor``.
- **DAG** (everything else) — ``policy_name`` is deliberately left ``None`` on
  ``ModelOnboardingInput``. Upstream ``handle_onboarding`` branches on
  ``policy_name is not None``, *not* on ``policy_type``, so forwarding it for a
  DAG policy would silently reroute every ordinary run into the interactive
  executor.

Architecture note:
    This handler wraps the omnibase_infra onboarding library and orchestrator
    logic directly via imported handler functions and models. It does NOT invoke
    the node_onboarding_orchestrator as an external runtime dependency.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from omnibase_infra.nodes.node_onboarding_orchestrator.handlers.handler_onboarding import (
    handle_onboarding,
)
from omnibase_infra.nodes.node_onboarding_orchestrator.models.model_onboarding_input import (
    ModelOnboardingInput,
)
from omnibase_infra.nodes.node_onboarding_orchestrator.models.model_onboarding_output import (
    ModelOnboardingOutput,
)
from omnibase_infra.onboarding.adapter_cli_input import AdapterCliInput
from omnibase_infra.onboarding.loader import load_canonical_graph
from omnibase_infra.onboarding.policy_resolver import (
    load_builtin_policies,
    load_policy_yaml,
    resolve_policy,
)

from omnimarket.nodes.node_onboarding.models.model_onboarding_start_command import (
    ModelOnboardingStartCommand,
)

# TODO(OMN-8270): remove _LOCAL_POLICIES_DIR + _load_local_policies after
# omnimarket pins omnibase_infra >= 0.34.0 and the upstream wheel ships
# new_employee.yaml.
_LOCAL_POLICIES_DIR = Path(__file__).resolve().parent.parent / "policies"


def _load_local_policies() -> dict[str, dict[str, Any]]:
    """Load local fallback policies shipped with this node.

    Returns a dict keyed by policy_name, same shape as load_builtin_policies().
    Empty dict if the local policies directory is missing.
    """
    result: dict[str, dict[str, Any]] = {}
    if not _LOCAL_POLICIES_DIR.is_dir():
        return result
    for path in sorted(_LOCAL_POLICIES_DIR.glob("*.yaml")):
        data = load_policy_yaml(path)
        name = data.get("policy_name")
        if isinstance(name, str):
            result[name] = cast(dict[str, Any], data)
    return result


def _resolve_policies() -> dict[str, dict[str, Any]]:
    """Merge upstream + local fallback policies; local takes precedence.

    A yaml shipped with this node can fill gaps in older omnibase_infra
    wheels (see the OMN-8270 removal note above).
    """
    policies: dict[str, dict[str, Any]] = dict(load_builtin_policies())
    policies.update(_load_local_policies())
    return policies


class HandlerOnboarding:
    """Compute handler for node_onboarding.

    Resolves policy name → target_capabilities, constructs ModelOnboardingInput,
    and delegates to handle_onboarding via await.
    """

    async def handle(self, command: ModelOnboardingStartCommand) -> dict[str, Any]:
        """Execute onboarding with the given command.

        Args:
            command: Onboarding start command with policy name or capabilities.

        Returns:
            Dict with success, total_steps, completed_steps, rendered_output.
            In dry_run mode, also includes dry_run=True and resolved_steps.

        Raises:
            ValueError: If policy_name is not found in builtin policies.
        """
        policies = _resolve_policies()
        policy_data = policies.get(command.policy_name)

        # Resolve target capabilities
        target_capabilities = list(command.target_capabilities)
        if not target_capabilities:
            if policy_data is None:
                msg = f"Unknown policy: {command.policy_name!r}. Available: {sorted(policies)}"
                raise ValueError(msg)
            target_capabilities = list(policy_data["target_capabilities"])

        # Interactive path: forward policy_name + output paths and inject the
        # CLI adapter. This is the only thing that makes the upstream
        # interactive executor reachable — see the module docstring.
        if policy_data is not None and policy_data.get("policy_type") == "interactive":
            interactive_input = ModelOnboardingInput(
                policy_name=command.policy_name,
                target_capabilities=target_capabilities,
                skip_steps=command.skip_steps or [],
                continue_on_failure=command.continue_on_failure,
                dry_run=command.dry_run,
                env_output_path=command.env_output_path,
                overlay_output_path=command.overlay_output_path,
            )
            interactive_output = cast(
                ModelOnboardingOutput,
                await handle_onboarding(
                    interactive_input, input_adapter=AdapterCliInput()
                ),
            )
            return cast(dict[str, Any], interactive_output.model_dump())

        # Dry-run: resolve and print plan without executing verifications
        if command.dry_run:
            graph = load_canonical_graph()
            steps = resolve_policy(
                graph, target_capabilities, command.skip_steps or None
            )
            plan = [s.step_key for s in steps]
            return {
                "success": True,
                "dry_run": True,
                "execution_mode": "verify-only",
                "requested_capabilities": target_capabilities,
                "resolved_steps": plan,
                "produced_capabilities_per_step": {
                    s.step_key: s.produces_capabilities for s in steps
                },
                "total_steps": len(plan),
                "completed_steps": 0,
                "rendered_output": f"Dry run — would execute {len(plan)} steps: {plan}",
            }

        # Execute onboarding
        input_model = ModelOnboardingInput(
            target_capabilities=target_capabilities,
            skip_steps=command.skip_steps or [],
            continue_on_failure=command.continue_on_failure,
        )
        output = cast(ModelOnboardingOutput, await handle_onboarding(input_model))
        return cast(dict[str, Any], output.model_dump())


__all__ = ["HandlerOnboarding"]
