# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden walker paths of the delegation orchestrator, one real chain each (OMN-19713).

Each case names its walker obligation with ``chain_obligation``. The path id
lists the orchestrator triggers in order (walker_report.json), and the case
pins both the events the real handler emits and the workflow state after
every step. The handler does not record which transition fired, so the state
sequence and the escalation count are what tie the case to its path.
"""

from __future__ import annotations

import pytest
from omnibase_core.enums.enum_agent_task_lifecycle_type import (
    EnumAgentTaskLifecycleType,
)
from omnibase_core.models.delegation.wire import EnumDelegationContentVerdict

from omnimarket.nodes.node_delegation_orchestrator.enums import EnumDelegationState
from tests.chains.chain_assert import assert_chain, chain_obligation
from tests.chains.delegation import _builders as b

pytestmark = pytest.mark.unit

S = EnumDelegationState


@chain_obligation("golden:invocation_command_accepted>early_lifecycle_completed")
async def test_remote_agent_completes_before_any_progress() -> None:
    cid, run = await b.drive(
        [
            lambda h, c: h.handle_delegation_request(b.request(c)),
            lambda h, c: h.handle_invocation_command(b.invocation_command(c)),
            lambda h, c: h.handle_agent_task_lifecycle(
                b.lifecycle(c, EnumAgentTaskLifecycleType.COMPLETED)
            ),
        ]
    )
    assert run.states == (S.RECEIVED, S.ROUTED, S.COMPLETED)
    assert_chain(
        run.events,
        expected_event_types=[
            "ModelRoutingIntent",
            "ModelInvocationCommand",
            "ModelDelegationCompleted",
        ],
        terminal_fields={
            "quality_passed": True,
            "content_verdict": EnumDelegationContentVerdict.USABLE,
        },
        correlation_id=cid,
        bus_history_count=run.bus_history_count,
    )


@chain_obligation(
    "golden:invocation_command_accepted>lifecycle_progress_received>lifecycle_completed"
)
async def test_remote_agent_progresses_then_completes() -> None:
    cid, run = await b.drive(
        [
            lambda h, c: h.handle_delegation_request(b.request(c)),
            lambda h, c: h.handle_invocation_command(b.invocation_command(c)),
            lambda h, c: h.handle_agent_task_lifecycle(
                b.lifecycle(c, EnumAgentTaskLifecycleType.PROGRESS)
            ),
            lambda h, c: h.handle_agent_task_lifecycle(
                b.lifecycle(c, EnumAgentTaskLifecycleType.COMPLETED)
            ),
        ]
    )
    assert run.states == (S.RECEIVED, S.ROUTED, S.EXECUTING, S.COMPLETED)
    assert_chain(
        run.events,
        expected_event_types=[
            "ModelRoutingIntent",
            "ModelInvocationCommand",
            "ModelDelegationCompleted",
        ],
        terminal_fields={
            "quality_passed": True,
            "content_verdict": EnumDelegationContentVerdict.USABLE,
        },
        correlation_id=cid,
        bus_history_count=run.bus_history_count,
    )


@chain_obligation(
    "golden:invocation_command_accepted>inference_error_escalation_available>"
    "escalation_tier_selected>early_lifecycle_completed"
)
async def test_inference_error_escalates_then_completes() -> None:
    # An empty backend ref on the first route, so there is no same-tier sibling to
    # retry and the retryable error escalates to the next tier.
    cid, run = await b.drive(
        [
            lambda h, c: h.handle_delegation_request(b.request(c)),
            lambda h, c: h.handle_routing_decision(
                b.routing_decision(c, tier_name="cheap_cloud", backend_ref="")
            ),
            lambda h, c: h.handle_inference_response(
                b.inference_error(c, "connection refused")
            ),
            lambda h, c: h.handle_agent_task_lifecycle(
                b.lifecycle(c, EnumAgentTaskLifecycleType.COMPLETED)
            ),
        ]
    )
    assert run.states == (S.RECEIVED, S.ROUTED, S.ROUTED, S.COMPLETED)
    assert run.escalation_count == 1
    assert_chain(
        run.events,
        expected_event_types=[
            "ModelRoutingIntent",
            "ModelInferenceIntent",
            "ModelRoutingIntent",
            "ModelLlmDelegationEscalationTriggeredEvent",
            "ModelDelegationCompleted",
        ],
        terminal_fields={
            "quality_passed": True,
            "content_verdict": EnumDelegationContentVerdict.USABLE,
        },
        correlation_id=cid,
        bus_history_count=run.bus_history_count,
    )


@chain_obligation(
    "golden:invocation_command_accepted>inference_response_received>"
    "gate_result_received>gate_failed_escalation_available>"
    "escalation_tier_selected>early_lifecycle_completed"
)
async def test_gate_failure_escalates_then_completes() -> None:
    # cheap_cloud is a paid tier, so a sub-bar gate result escalates instead
    # of retrying on the same tier.
    cid, run = await b.drive(
        [
            lambda h, c: h.handle_delegation_request(b.request(c)),
            lambda h, c: h.handle_routing_decision(
                b.routing_decision(c, tier_name="cheap_cloud")
            ),
            lambda h, c: h.handle_inference_response(b.inference_ok(c)),
            lambda h, c: h.handle_gate_result(b.gate_result(c, passed=False)),
            lambda h, c: h.handle_agent_task_lifecycle(
                b.lifecycle(c, EnumAgentTaskLifecycleType.COMPLETED)
            ),
        ]
    )
    assert run.states == (
        S.RECEIVED,
        S.ROUTED,
        S.INFERENCE_COMPLETED,
        S.ROUTED,
        S.COMPLETED,
    )
    assert run.escalation_count == 1
    assert_chain(
        run.events,
        expected_event_types=[
            "ModelRoutingIntent",
            "ModelInferenceIntent",
            "ModelQualityGateIntent",
            "ModelRoutingIntent",
            "ModelLlmDelegationEscalationTriggeredEvent",
            "ModelDelegationCompleted",
        ],
        terminal_fields={
            "quality_passed": True,
            "content_verdict": EnumDelegationContentVerdict.USABLE,
        },
        correlation_id=cid,
        bus_history_count=run.bus_history_count,
    )


@chain_obligation(
    "golden:invocation_command_accepted>inference_response_received>"
    "gate_result_received>gate_failed_local_retry>early_lifecycle_completed"
)
async def test_gate_failure_on_a_free_tier_retries_locally_then_completes() -> None:
    # local is a free tier: a sub-bar result is retried on the same tier
    # (OMN-14234) and the escalation count does not move.
    cid, run = await b.drive(
        [
            lambda h, c: h.handle_delegation_request(b.request(c)),
            lambda h, c: h.handle_routing_decision(
                b.routing_decision(c, tier_name="local")
            ),
            lambda h, c: h.handle_inference_response(b.inference_ok(c)),
            lambda h, c: h.handle_gate_result(b.gate_result(c, passed=False)),
            lambda h, c: h.handle_agent_task_lifecycle(
                b.lifecycle(c, EnumAgentTaskLifecycleType.COMPLETED)
            ),
        ]
    )
    assert run.states == (
        S.RECEIVED,
        S.ROUTED,
        S.INFERENCE_COMPLETED,
        S.ROUTED,
        S.COMPLETED,
    )
    assert run.escalation_count == 0
    assert_chain(
        run.events,
        expected_event_types=[
            "ModelRoutingIntent",
            "ModelInferenceIntent",
            "ModelQualityGateIntent",
            "ModelRoutingIntent",
            "ModelDelegationCompleted",
        ],
        terminal_fields={
            "quality_passed": True,
            "content_verdict": EnumDelegationContentVerdict.USABLE,
        },
        correlation_id=cid,
        bus_history_count=run.bus_history_count,
    )
