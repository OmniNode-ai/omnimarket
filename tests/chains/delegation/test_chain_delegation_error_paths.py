# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error walker paths of the delegation orchestrator, one real chain each (OMN-19713).

The fifth error path, the routing-leg boundary failure, is bound in
``test_chain_delegation_orchestrator.py``.
"""

from __future__ import annotations

import pytest
from omnibase_core.enums.enum_agent_task_lifecycle_type import (
    EnumAgentTaskLifecycleType,
)
from omnibase_core.enums.enum_delegation_terminal_outcome import (
    EnumDelegationTerminalOutcome,
)
from omnibase_core.models.delegation.wire import EnumDelegationOperationalOutcome

from omnimarket.nodes.node_delegation_orchestrator.contract_topics import (
    TOPIC_ID_QUALITY_GATE_REQUEST,
)
from omnimarket.nodes.node_delegation_orchestrator.enums import EnumDelegationState
from tests.chains.chain_assert import assert_error_chain, chain_obligation
from tests.chains.delegation import _builders as b

pytestmark = pytest.mark.unit

S = EnumDelegationState


@chain_obligation(
    "error:invocation_command_accepted>lifecycle_progress_received>lifecycle_failed"
)
async def test_remote_agent_progresses_then_fails() -> None:
    cid, run = await b.drive(
        [
            lambda h, c: h.handle_delegation_request(b.request(c)),
            lambda h, c: h.handle_invocation_command(b.invocation_command(c)),
            lambda h, c: h.handle_agent_task_lifecycle(
                b.lifecycle(c, EnumAgentTaskLifecycleType.PROGRESS)
            ),
            lambda h, c: h.handle_agent_task_lifecycle(
                b.lifecycle(
                    c, EnumAgentTaskLifecycleType.FAILED, error="remote agent crashed"
                )
            ),
        ]
    )
    assert run.states == (S.RECEIVED, S.ROUTED, S.EXECUTING, S.FAILED)
    assert_error_chain(
        run.events,
        expected_event_types=[
            "ModelRoutingIntent",
            "ModelInvocationCommand",
            "ModelDelegationFailed",
        ],
        terminal_fields={
            "failure_reason": "remote agent crashed",
            "quality_passed": False,
        },
        correlation_id=cid,
        bus_history_count=run.bus_history_count,
    )


@chain_obligation(
    "error:invocation_command_accepted>inference_response_received>"
    "gate_result_received>gate_failed"
)
async def test_gate_failure_with_no_escalation_budget_fails() -> None:
    cid, run = await b.drive(
        [
            lambda h, c: h.handle_delegation_request(b.request(c)),
            lambda h, c: h.handle_routing_decision(
                b.routing_decision(c, tier_name="cheap_cloud")
            ),
            lambda h, c: h.handle_inference_response(b.inference_ok(c)),
            lambda h, c: h.handle_gate_result(
                b.gate_result(c, passed=False), max_escalation_attempts=0
            ),
        ]
    )
    assert run.states == (S.RECEIVED, S.ROUTED, S.INFERENCE_COMPLETED, S.FAILED)
    assert_error_chain(
        run.events,
        expected_event_types=[
            "ModelRoutingIntent",
            "ModelInferenceIntent",
            "ModelQualityGateIntent",
            "ModelDelegationFailed",
            "ModelDelegationTerminalFailedRoutedV2",
        ],
        terminal_fields={"terminal_outcome": EnumDelegationTerminalOutcome.FAILED},
        correlation_id=cid,
        bus_history_count=run.bus_history_count,
    )


@chain_obligation(
    "error:invocation_command_accepted>inference_response_received>"
    "gate_boundary_terminalized"
)
async def test_quality_gate_leg_boundary_failure_fails() -> None:
    cid, run = await b.drive(
        [
            lambda h, c: h.handle_delegation_request(b.request(c)),
            lambda h, c: h.handle_routing_decision(
                b.routing_decision(c, tier_name="cheap_cloud")
            ),
            lambda h, c: h.handle_inference_response(b.inference_ok(c)),
            lambda h, c: h.handle_boundary_failure_terminal(
                b.boundary_failure(c, origin_topic=TOPIC_ID_QUALITY_GATE_REQUEST)
            ),
        ]
    )
    assert run.states == (S.RECEIVED, S.ROUTED, S.INFERENCE_COMPLETED, S.FAILED)
    assert_error_chain(
        run.events,
        expected_event_types=[
            "ModelRoutingIntent",
            "ModelInferenceIntent",
            "ModelQualityGateIntent",
            "ModelDelegationFailed",
        ],
        terminal_fields={
            "failure_reason": "leg configuration did not resolve",
            "quality_passed": False,
        },
        correlation_id=cid,
        bus_history_count=run.bus_history_count,
    )


@chain_obligation("error:invocation_command_accepted>terminal_failure_no_escalation")
@pytest.mark.usefixtures("stub_provider_quota_reader")
async def test_inference_error_at_the_ceiling_fails_without_escalating() -> None:
    # "empty choices array" is a no-answer failure that can move, so this path
    # starts at the declared ceiling tier, where there is no later responder.
    # With an empty backend ref there is no same-tier sibling either, so the
    # workflow terminalises.
    cid, run = await b.drive(
        [
            lambda h, c: h.handle_delegation_request(b.request(c)),
            lambda h, c: h.handle_routing_decision(
                b.routing_decision(c, tier_name="claude", backend_ref="")
            ),
            lambda h, c: h.handle_inference_response(
                b.inference_error(c, "empty choices array")
            ),
        ]
    )
    assert run.states == (S.RECEIVED, S.ROUTED, S.FAILED)
    assert run.escalation_count == 0
    assert_error_chain(
        run.events,
        expected_event_types=[
            "ModelRoutingIntent",
            "ModelInferenceIntent",
            "ModelDelegationFailed",
        ],
        terminal_fields={
            "failure_reason": "empty choices array",
            "terminal_failure_reason": (
                "no_higher_tier_available: task_class='test' exhausted escalation "
                "policy tier_order=['local', 'cheap_frontier', 'cheap_cloud', "
                "'claude'] \u2014 current_tier='claude' is the final (ceiling) "
                "tier; no higher tier is declared for this task class"
            ),
            "operational_outcome": EnumDelegationOperationalOutcome.INFERENCE_FAILED,
        },
        correlation_id=cid,
        bus_history_count=run.bus_history_count,
    )
