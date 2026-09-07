# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wire-shape invariants for the redeploy FSM (OMN-16939).

Three defects found by reading the dev-lane broker rather than the tests, on
2026-09-06. Each is pinned here as an invariant so it cannot come back.

D3 -- ONE logical rollback must produce ONE broker record.
    ``HandlerDeployPublishMonitor.rollback`` published the bare payload itself
    AND ``handle`` returned the same fact for the runtime to publish, so
    ``onex.evt.omnimarket.redeploy-rolled-back.v1`` carried two records per
    rollback a few ms apart (offsets 0/1, 2/3, 10/11 -- bare then envelope).
    ``node_redeploy_orchestrator`` terminalizes each arrival, so that became
    two ``redeploy-completed`` terminals per run (offsets 6378/6379,
    6380/6381, 6382/6383, each with a distinct ``envelope_timestamp``).

D6 -- the gate decision must carry ``deploy_context`` on EVERY branch.
    (The reported "offset 99 has no deploy_context" was a DLQ replay of the
    pre-fix record at offset 93 -- same ``envelope_timestamp``,
    ``x-replayed-by: node_dlq_replay_effect``. Every record actually produced
    after the fix carries it. The invariant is asserted here anyway, because
    the echo lives at one call site and a future refactor could move it into
    the branches.)

D7 -- the rebuild command's envelope header correlation must equal its payload
    correlation. It did not: payload ``86d5da00-...`` against header
    ``ce9eaa2e-...`` on ``onex.cmd.deploy.rebuild-requested.v1`` offset 0.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.events.runtime_deployment import (
    EnumRuntimeLane,
    ModelRedeployRolledBackEvent,
)
from omnimarket.nodes.node_prod_promotion_gate_compute.handlers.handler_prod_promotion_gate import (
    evaluate_gate,
)
from omnimarket.nodes.node_redeploy_deploy_effect.handlers.handler_deploy_publish_monitor import (
    TOPIC_REBUILD_REQUESTED,
    TOPIC_ROLLED_BACK,
    HandlerDeployPublishMonitor,
)
from omnimarket.nodes.node_redeploy_deploy_effect.models.model_deploy_publish_command import (
    ModelDeployPublishCommand,
)
from omnimarket.testing.publisher_contract_fixture import publisher_event_type


def _envelope(command: ModelDeployPublishCommand) -> ModelEventEnvelope[Any]:
    return ModelEventEnvelope(
        payload=command,
        correlation_id=command.correlation_id,
        # OMN-18013: derive the event type from the publisher's contract rather
        # than hand-typing the topic. The bus carries the alias
        # <producer>.<event-name>; the full topic string is a spelling the
        # runtime never stamps, and asserting on it makes a chain green on a
        # message that cannot exist.
        event_type=publisher_event_type(
            "onex.cmd.omnimarket.redeploy-deploy-publish.v1"
        ),
    )


@pytest.mark.unit
class TestOneTerminalPerRollback:
    async def test_handler_emits_the_rolled_back_fact_and_publishes_none_itself(
        self,
    ) -> None:
        """D3: exactly one carrier of the rollback fact leaves the handler.

        RED before the fix: ``published`` had one entry AND ``output.events``
        had one entry, i.e. two records on one topic on the real runtime.
        """
        bus = EventBusInmemory(environment="test", group="d3")
        await bus.start()
        published: list[dict[str, Any]] = []

        async def _capture(message: object) -> None:
            published.append(json.loads(message.value))  # type: ignore[union-attr]

        await bus.subscribe(
            TOPIC_ROLLED_BACK, on_message=_capture, group_id="d3-capture"
        )

        handler = HandlerDeployPublishMonitor(event_bus=bus, timeout_s=0.1)
        command = ModelDeployPublishCommand(
            correlation_id=uuid4(), runtime_lane=EnumRuntimeLane.DEV
        )
        output = await handler.handle(_envelope(command))

        assert published == [], (
            "the handler must not publish its own terminal: the runtime "
            "publishes ModelHandlerOutput.events, so a direct publish makes "
            "two broker records per logical rollback"
        )
        assert len(output.events) == 1
        assert isinstance(output.events[0].payload, ModelRedeployRolledBackEvent)
        assert output.events[0].event_type == TOPIC_ROLLED_BACK

        await bus.close()

    def test_rollback_is_a_pure_builder(self) -> None:
        """``rollback`` is synchronous and touches no bus.

        A coroutine that publishes is one edit away from being re-awaited in a
        retry path; a plain builder cannot double-publish by construction.
        """

        class _RefusingBus:
            async def publish(self, *args: object, **kwargs: object) -> None:
                raise AssertionError("rollback must not publish")

        handler = HandlerDeployPublishMonitor(event_bus=_RefusingBus())
        event = handler.rollback(
            correlation_id=uuid4(),
            runtime_lane=EnumRuntimeLane.DEV,
            restored_image="omninode-runtime:v2.3.1",
            failure_reason="deploy agent timed out before completion; rolling back",
            failed_phase=__import__(
                "omnimarket.events.runtime_deployment", fromlist=["EnumRedeployPhase"]
            ).EnumRedeployPhase.VERIFY_HEALTH,
        )
        assert isinstance(event, ModelRedeployRolledBackEvent)


@pytest.mark.unit
class TestRebuildCommandCorrelationIsNotSplit:
    async def test_header_correlation_equals_payload_correlation(self) -> None:
        """D7: the envelope header must carry the FSM run's correlation.

        RED before the fix: the bus minted a fresh header correlation because
        ``publish`` was called with no ``headers``, so the deploy agent's work
        could not be joined back to the run that asked for it.
        """
        bus = EventBusInmemory(environment="test", group="d7")
        await bus.start()
        seen: list[tuple[Any, Any]] = []

        async def _capture(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            headers = message.headers  # type: ignore[union-attr]
            seen.append((payload["correlation_id"], headers.correlation_id))

        await bus.subscribe(
            TOPIC_REBUILD_REQUESTED, on_message=_capture, group_id="d7-capture"
        )

        corr = uuid4()
        handler = HandlerDeployPublishMonitor(event_bus=bus, timeout_s=0.1)
        await handler.handle(
            _envelope(
                ModelDeployPublishCommand(
                    correlation_id=corr, runtime_lane=EnumRuntimeLane.DEV
                )
            )
        )

        assert len(seen) == 1
        payload_corr, header_corr = seen[0]
        assert UUID(str(payload_corr)) == corr
        assert UUID(str(header_corr)) == corr, (
            "header correlation must equal payload correlation; a split makes "
            "the deploy agent's work unjoinable to the FSM run"
        )

        await bus.close()


@pytest.mark.unit
class TestGateDecisionAlwaysCarriesDeployContext:
    """D6: the echo is a property of ``evaluate_gate``, not of a branch.

    The gate has several return points. ``evaluate_gate`` echoes
    ``deploy_context`` once, AFTER ``_decide_gate`` returns, so the echo cannot
    depend on which branch fired. These tests hold that seam in place: the
    first drives real decisions through the public surface, the second forces
    every possible decision shape by stubbing ``_decide_gate``.
    """

    @pytest.mark.parametrize(
        "lane",
        [EnumRuntimeLane.DEV, EnumRuntimeLane.STABILITY_TEST, EnumRuntimeLane.PROD],
    )
    def test_real_decisions_echo_the_context(self, lane: EnumRuntimeLane) -> None:
        from omnimarket.events.runtime_deployment import (
            EnumRedeployScope,
            ModelProdPromotionGateCommand,
            ModelRedeployDeployContext,
        )

        context = ModelRedeployDeployContext(
            scope=EnumRedeployScope.FULL,
            git_ref="751d685bd6bc784c11dc0af17c564d6f05ab4886",
            runtime_lane=lane,
        )
        command = ModelProdPromotionGateCommand(
            correlation_id=uuid4(), runtime_lane=lane, deploy_context=context
        )

        decision = evaluate_gate(command)

        # dev/stability are allowed, prod with no grant is denied -- both
        # branches must still carry the context.
        assert decision.deploy_context == context, (
            f"lane={lane.value} allowed={decision.allowed}: the decision is the "
            "only thing that rides back over the bus, so a decision without "
            "deploy_context makes the orchestrator issue a DEFAULTED deploy"
        )

    def test_every_decision_shape_is_echoed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Positive control on the seam itself, independent of gate policy."""
        from omnimarket.events.runtime_deployment import (
            EnumRedeployScope,
            ModelProdPromotionGateCommand,
            ModelProdPromotionGateDecision,
            ModelRedeployDeployContext,
        )
        from omnimarket.nodes.node_prod_promotion_gate_compute.handlers import (
            handler_prod_promotion_gate as gate_module,
        )

        context = ModelRedeployDeployContext(
            scope=EnumRedeployScope.RUNTIME,
            git_ref="origin/dev",
            runtime_lane=EnumRuntimeLane.DEV,
        )
        command = ModelProdPromotionGateCommand(
            correlation_id=uuid4(),
            runtime_lane=EnumRuntimeLane.DEV,
            deploy_context=context,
        )

        shapes = (
            (True, "allowed", None, None),
            (False, "denied: no grant", None, None),
            (True, "allowed with digest", "sha256:abc", "omninode-runtime:v2.3.1"),
        )
        for allowed, reason, digest, rollback in shapes:
            monkeypatch.setattr(
                gate_module,
                "_decide_gate",
                lambda _cmd, _a=allowed, _r=reason, _d=digest, _t=rollback: (
                    ModelProdPromotionGateDecision(
                        allowed=_a, reason=_r, image_digest=_d, rollback_target=_t
                    )
                ),
            )
            echoed = gate_module.evaluate_gate(command)
            assert echoed.deploy_context == context, (allowed, reason)
            # the echo must not disturb the decision itself
            assert echoed.allowed is allowed
            assert echoed.reason == reason


@pytest.mark.unit
class TestEffectWireTopicsArePinned:
    """The effect's two output states, pinned to their wire literals.

    These constants are resolved from ``contract.yaml`` at import time, so the
    handler has no hardcoded topic strings — which also means a contract typo
    would silently repoint a live publish. Asserting the resolved value against
    the literal is what makes the contract the authority AND keeps the wire
    address under test. (Both states were declared and unasserted until now:
    the coverage gate only checks nodes a change touches, so the gap surfaced
    the first time this node was edited.)
    """

    def test_rebuild_command_topic(self) -> None:
        assert TOPIC_REBUILD_REQUESTED == "onex.cmd.deploy.rebuild-requested.v1"

    def test_deploy_refused_topic(self) -> None:
        from omnimarket.nodes.node_redeploy_deploy_effect.handlers.handler_deploy_publish_monitor import (
            TOPIC_DEPLOY_REFUSED,
        )

        assert TOPIC_DEPLOY_REFUSED == "onex.evt.omnimarket.redeploy-deploy-refused.v1"

    async def test_refused_prod_deploy_lands_on_the_refused_topic(self) -> None:
        """A prod command with no grant publishes ONLY the refusal.

        Positive control for the topic constant above: the address is exercised,
        not merely compared.
        """
        from omnimarket.nodes.node_redeploy_deploy_effect.handlers.handler_deploy_publish_monitor import (
            TOPIC_DEPLOY_REFUSED,
        )

        bus = EventBusInmemory(environment="test", group="refused")
        await bus.start()
        refused: list[dict[str, Any]] = []
        rebuilds: list[dict[str, Any]] = []

        async def _on_refused(message: object) -> None:
            refused.append(json.loads(message.value))  # type: ignore[union-attr]

        async def _on_rebuild(message: object) -> None:
            rebuilds.append(json.loads(message.value))  # type: ignore[union-attr]

        await bus.subscribe(
            TOPIC_DEPLOY_REFUSED, on_message=_on_refused, group_id="refused-capture"
        )
        await bus.subscribe(
            TOPIC_REBUILD_REQUESTED, on_message=_on_rebuild, group_id="rebuild-capture"
        )

        handler = HandlerDeployPublishMonitor(event_bus=bus, timeout_s=0.1)
        corr = uuid4()
        output = await handler.handle(
            _envelope(
                ModelDeployPublishCommand(
                    correlation_id=corr,
                    runtime_lane=EnumRuntimeLane.PROD,
                    image_digest="sha256:deadbeef",
                )
            )
        )

        assert len(refused) == 1
        assert UUID(str(refused[0]["correlation_id"])) == corr
        assert rebuilds == [], "a refused prod deploy must never reach the agent"
        assert len(output.events) == 1
        assert output.events[0].event_type == TOPIC_DEPLOY_REFUSED

        await bus.close()
