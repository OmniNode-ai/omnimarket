# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Mandatory DI param enforcement tests for omnimarket handlers (OMN-10278).

Asserts that injectable event_bus params are NOT optional — constructing
handlers without event_bus must raise TypeError, not silently accept None.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytestmark = [pytest.mark.unit]


def test_handler_aislop_sweep_requires_event_bus() -> None:
    from omnimarket.nodes.node_aislop_sweep.handlers.handler_aislop_sweep import (
        NodeAislopSweep,
    )

    with pytest.raises(TypeError):
        NodeAislopSweep()  # type: ignore[call-arg]


def test_handler_autopilot_orchestrator_requires_event_bus() -> None:
    from omnimarket.nodes.node_autopilot_orchestrator.handlers.handler_autopilot_orchestrator import (
        HandlerAutopilotOrchestrator,
    )

    with pytest.raises(TypeError):
        HandlerAutopilotOrchestrator()  # type: ignore[call-arg]


def test_handler_build_loop_orchestrator_requires_event_bus() -> None:
    from omnimarket.nodes.node_build_loop_orchestrator.handlers.handler_build_loop_orchestrator import (
        HandlerBuildLoopOrchestrator,
    )

    with pytest.raises(TypeError):
        HandlerBuildLoopOrchestrator()  # type: ignore[call-arg]


def test_handler_local_supervisor_requires_event_bus() -> None:
    from omnimarket.nodes.node_local_supervisor.handlers.handler_local_supervisor import (
        HandlerLocalSupervisor,
    )

    with pytest.raises(TypeError):
        HandlerLocalSupervisor()  # type: ignore[call-arg]


def test_handler_model_router_boot_constructible_then_fails_loudly() -> None:
    """OMN-12429: node_model_router is in_process / subscribe_topics: [] — the
    runtime auto-wires it at boot by injecting only event_bus (policy/registry
    are caller-supplied config). So unlike the dispatched handlers above, this
    handler MUST be constructible without policy/registry, but MUST fail loudly
    (not silently route to a default) if anyone routes through that boot
    instance. This is the inverse guard: constructible, never silently usable.
    """
    import asyncio

    from omnimarket.nodes.node_model_router.handlers.handler_model_router import (
        HandlerModelRouter,
    )
    from omnimarket.nodes.node_model_router.models.model_routing_request import (
        ModelRoutingRequest,
    )

    # Boot-time construction with only event_bus must succeed (no TypeError):
    # this is exactly the runtime auto-wiring resolver's known-param path.
    router = HandlerModelRouter(
        event_bus=MagicMock(),  # transport-mock-ok: HandlerModelRouter.event_bus is typed Any — spec= cannot be applied
    )

    # Routing through an unconfigured instance must fail loudly, not silently.
    with pytest.raises(RuntimeError, match="without policy/registry"):
        asyncio.run(
            router.route_async(
                ModelRoutingRequest(prompt="x", role="r", correlation_id="c")
            )
        )


def test_handler_overnight_requires_event_bus() -> None:
    from omnimarket.nodes.node_overnight.handlers.handler_overnight import (
        HandlerBuildLoopExecutor,
    )

    with pytest.raises(TypeError):
        HandlerBuildLoopExecutor()  # type: ignore[call-arg]


def test_handler_overseer_verifier_consumer_requires_event_bus() -> None:
    from omnimarket.nodes.node_overseer_verifier.handlers.handler_overseer_verifier_consumer import (
        HandlerOverseerVerifierConsumer,
    )

    with pytest.raises(TypeError):
        HandlerOverseerVerifierConsumer()  # type: ignore[call-arg]


def test_handler_pipeline_fill_requires_event_bus() -> None:
    from omnimarket.nodes.node_pipeline_fill.handlers.handler_pipeline_fill import (
        HandlerPipelineFill,
    )

    with pytest.raises(TypeError):
        HandlerPipelineFill()  # type: ignore[call-arg]


def test_handler_pr_lifecycle_orchestrator_requires_event_bus() -> None:
    from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
        HandlerPrLifecycleOrchestrator,
    )

    with pytest.raises(TypeError):
        HandlerPrLifecycleOrchestrator()  # type: ignore[call-arg]


def test_handler_deploy_publish_monitor_requires_event_bus() -> None:
    from omnimarket.nodes.node_redeploy_deploy_effect.handlers.handler_deploy_publish_monitor import (
        HandlerDeployPublishMonitor,
    )

    with pytest.raises(TypeError):
        HandlerDeployPublishMonitor()  # type: ignore[call-arg]


def test_handler_review_thread_reconciler_requires_event_bus() -> None:
    from omnimarket.nodes.node_review_thread_reconciler.handlers.handler_review_thread_reconciler import (
        HandlerReviewThreadReconciler,
    )

    with pytest.raises(TypeError):
        HandlerReviewThreadReconciler()  # type: ignore[call-arg]


def test_handler_session_compose_requires_event_bus() -> None:
    from omnimarket.nodes.node_session_compose.handlers.handler_session_compose import (
        HandlerSessionCompose,
    )

    with pytest.raises(TypeError):
        HandlerSessionCompose()  # type: ignore[call-arg]


def test_handler_skill_requested_requires_event_bus() -> None:
    from omnimarket.nodes.node_skill_dispatch_engine_orchestrator.handlers.handler_skill_requested import (
        HandlerSkillRequested,
    )

    with pytest.raises(TypeError):
        HandlerSkillRequested()  # type: ignore[call-arg]


def test_handler_swarm_supervisor_orchestrator_requires_event_bus() -> None:
    from omnimarket.nodes.node_swarm_supervisor_orchestrator.handlers.handler_swarm_supervisor_orchestrator import (
        HandlerSwarmSupervisorOrchestrator,
    )

    with pytest.raises(TypeError):
        HandlerSwarmSupervisorOrchestrator()  # type: ignore[call-arg]


def test_handler_version_skew_detector_requires_event_bus() -> None:
    from omnimarket.nodes.node_version_skew_detector.handlers.handler_version_skew_detector import (
        NodeVersionSkewDetector,
    )

    with pytest.raises(TypeError):
        NodeVersionSkewDetector()  # type: ignore[call-arg]
