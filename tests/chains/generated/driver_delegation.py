# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Trigger fixture table for node_delegation_orchestrator.

The generator drives a walker path by looking each trigger up here. An input
trigger names the handler call and fixture that fires it; a follow trigger is
fired inside the preceding call, so it adds no step but may choose that
call's fixture (a gate result that passes, a tier that retries locally). A
trigger in neither table makes the path undrivable, and the gate reports it as
a path with no chain. Fixtures are the pilot's builders (OMN-19713).
"""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from omnibase_core.enums.enum_agent_task_lifecycle_type import (
    EnumAgentTaskLifecycleType,
)

from omnimarket.nodes.node_delegation_orchestrator.contract_topics import (
    TOPIC_ID_QUALITY_GATE_REQUEST,
    TOPIC_ID_ROUTING_REQUEST,
)
from omnimarket.nodes.node_event_chain_generator_compute.handlers.handler_event_chain_generator import (
    UndrivablePathError,
)
from omnimarket.nodes.node_event_chain_generator_compute.models.model_chain_generation import (
    ModelChainObligation,
    ModelDrivenChain,
)
from tests.chains.delegation import _builders as b

L = EnumAgentTaskLifecycleType

# Fired inside the preceding input's handler call.
FOLLOW_TRIGGERS = frozenset(
    {
        "gate_passed",
        "gate_failed",
        "gate_failed_escalation_available",
        "gate_failed_local_retry",
        "escalation_tier_selected",
    }
)
# Triggers that need the route to go through inference rather than a remote agent.
_INFERENCE_TRIGGERS = frozenset(
    {
        "inference_response_received",
        "inference_error_escalation_available",
        "terminal_failure_no_escalation",
    }
)
# Triggers that need a route with no same-tier sibling to retry on.
_NO_SIBLING_TRIGGERS = frozenset(
    {"inference_error_escalation_available", "terminal_failure_no_escalation"}
)


def _route(triggers: tuple[str, ...]) -> b.Step:
    if not _INFERENCE_TRIGGERS.intersection(triggers):
        return lambda h, c: h.handle_invocation_command(b.invocation_command(c))
    # The pilot's tiers: a free tier retries locally, cheap_cloud escalates a
    # sub-bar result, and free_local accepts a passing gate result as final.
    if "gate_failed_local_retry" in triggers:
        tier = "local"
    elif "gate_passed" in triggers:
        tier = "free_local"
    else:
        tier = "cheap_cloud"
    backend_ref = "" if _NO_SIBLING_TRIGGERS.intersection(triggers) else b.BACKEND_REF
    return lambda h, c: h.handle_routing_decision(
        b.routing_decision(c, tier_name=tier, backend_ref=backend_ref)
    )


def _gate(triggers: tuple[str, ...]) -> b.Step:
    passed = "gate_passed" in triggers
    if "gate_failed" in triggers:
        return lambda h, c: h.handle_gate_result(
            b.gate_result(c, passed=False), max_escalation_attempts=0
        )
    return lambda h, c: h.handle_gate_result(b.gate_result(c, passed=passed))


def _always(step: b.Step) -> Callable[[tuple[str, ...]], b.Step]:
    return lambda _triggers: step


def _answer(triggers: tuple[str, ...]) -> b.Step:
    # A path that ends on a passing gate needs content the delegation contract
    # accepts as final: the pilot's ANSWER-headed response (OMN-19710).
    if "gate_passed" not in triggers:
        return lambda h, c: h.handle_inference_response(b.inference_ok(c))
    return lambda h, c: h.handle_inference_response(
        b.inference_ok(c).model_copy(
            update={"content": "### ANSWER\n" + b.inference_ok(c).content}
        )
    )


INPUT_TRIGGERS = {
    "invocation_command_accepted": _route,
    "inference_response_received": _answer,
    "inference_error_escalation_available": _always(
        lambda h, c: h.handle_inference_response(
            b.inference_error(c, "connection refused")
        )
    ),
    "terminal_failure_no_escalation": _always(
        lambda h, c: h.handle_inference_response(
            b.inference_error(c, "empty choices array")
        )
    ),
    "gate_result_received": _gate,
    "gate_boundary_terminalized": _always(
        lambda h, c: h.handle_boundary_failure_terminal(
            b.boundary_failure(c, origin_topic=TOPIC_ID_QUALITY_GATE_REQUEST)
        )
    ),
    "routing_boundary_terminalized": _always(
        lambda h, c: h.handle_boundary_failure_terminal(
            b.boundary_failure(c, origin_topic=TOPIC_ID_ROUTING_REQUEST)
        )
    ),
    "lifecycle_progress_received": _always(
        lambda h, c: h.handle_agent_task_lifecycle(b.lifecycle(c, L.PROGRESS))
    ),
    "lifecycle_completed": _always(
        lambda h, c: h.handle_agent_task_lifecycle(b.lifecycle(c, L.COMPLETED))
    ),
    "early_lifecycle_completed": _always(
        lambda h, c: h.handle_agent_task_lifecycle(b.lifecycle(c, L.COMPLETED))
    ),
    "lifecycle_failed": _always(
        lambda h, c: h.handle_agent_task_lifecycle(
            b.lifecycle(c, L.FAILED, error="remote agent crashed")
        )
    ),
}


class DelegationChainDriver:
    workflow_owner = "node_delegation_orchestrator"

    async def run(self, obligation: ModelChainObligation) -> tuple[UUID, b.ChainRun]:
        triggers = obligation.triggers
        steps: list[b.Step] = [lambda h, c: h.handle_delegation_request(b.request(c))]
        for trigger in triggers:
            if trigger in FOLLOW_TRIGGERS:
                continue
            fixture = INPUT_TRIGGERS.get(trigger)
            if fixture is None:
                raise UndrivablePathError(f"no fixture for trigger {trigger!r}")
            steps.append(fixture(triggers))
        return await b.drive(steps)

    async def drive(self, obligation: ModelChainObligation) -> ModelDrivenChain:
        _, run = await self.run(obligation)
        terminal = run.events[-1].payload
        return ModelDrivenChain(
            event_types=tuple(event.event_type for event in run.events),
            states=tuple(state.name for state in run.states),
            terminal_payload=terminal.model_dump(mode="json")
            if hasattr(terminal, "model_dump")
            else {},
        )


DRIVERS = {DelegationChainDriver.workflow_owner: DelegationChainDriver()}
