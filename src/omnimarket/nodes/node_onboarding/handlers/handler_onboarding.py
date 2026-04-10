# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Compute handler for node_onboarding (OMN-8273).

Resolves a policy name to target_capabilities, constructs ModelOnboardingInput,
and delegates to handle_onboarding via asyncio.run().

Architecture note:
    This handler wraps the omnibase_infra onboarding library and orchestrator
    logic directly via imported handler functions and models. It does NOT invoke
    the node_onboarding_orchestrator as an external runtime dependency.

asyncio.run() caveat:
    asyncio.run(handle_onboarding(input_model)) is correct for the current
    synchronous compute-node invocation path. This will break if the compute
    node is ever invoked inside an existing event loop context (e.g., from an
    async caller or under pytest-asyncio with asyncio_mode=auto). This is a
    known limitation for future async invocation paths.
"""

from __future__ import annotations

import asyncio
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
from omnibase_infra.onboarding.loader import load_canonical_graph
from omnibase_infra.onboarding.policy_resolver import (
    load_builtin_policies,
    resolve_policy,
)

from omnimarket.nodes.node_onboarding.models.model_onboarding_start_command import (
    ModelOnboardingStartCommand,
)


class HandlerOnboarding:
    """Compute handler for node_onboarding.

    Resolves policy name → target_capabilities, constructs ModelOnboardingInput,
    and delegates to handle_onboarding via asyncio.run().
    """

    def handle(self, command: ModelOnboardingStartCommand) -> dict[str, Any]:
        """Execute onboarding with the given command.

        Args:
            command: Onboarding start command with policy name or capabilities.

        Returns:
            Dict with success, total_steps, completed_steps, rendered_output.
            In dry_run mode, also includes dry_run=True and resolved_steps.

        Raises:
            ValueError: If policy_name is not found in builtin policies.
        """
        # Resolve target capabilities
        target_capabilities = list(command.target_capabilities)
        if not target_capabilities:
            policies = load_builtin_policies()
            policy_data = policies.get(command.policy_name)
            if policy_data is None:
                msg = f"Unknown policy: {command.policy_name!r}. Available: {sorted(policies)}"
                raise ValueError(msg)
            target_capabilities = list(policy_data["target_capabilities"])

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
                "resolved_steps": plan,
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
        output = cast(
            ModelOnboardingOutput, asyncio.run(handle_onboarding(input_model))
        )
        return cast(dict[str, Any], output.model_dump())


__all__ = ["HandlerOnboarding"]
