# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The redeploy orchestrator can actually receive the gate decision (OMN-16939).

WHAT WAS BROKEN, MEASURED ON THE .201 DEV LANE 2026-09-06
---------------------------------------------------------
``node_redeploy_orchestrator`` declared nine ``subscribe_topics`` under ONE
``operation_match`` handler entry with no ``message_category``. ``handler_wiring``
derives an entry's category once — explicit ``message_category``, else
``_derive_message_category(subscribe_topics[0])`` — and stamps it on EVERY route the
entry registers. ``subscribe_topics[0]`` is ``onex.cmd.omnimarket.redeploy-start.v1``,
so all nine routes registered under ``command`` and every ``.evt.`` topic, arriving as
``event``, matched zero routes.

Live consequence, read with ``rpk`` on compose project ``omnibase-infra``:

* ``onex.evt.omnimarket.prod-promotion-gate-evaluated.v1`` HIGH-WATERMARK 93,
* group ``local.omnimarket.node_redeploy_orchestrator...gate-evaluated.v1``
  ``Stable`` / ``MEMBERS 1`` / ``CURRENT-OFFSET 93`` / ``LAG 0``,
* ``onex.cmd.omnimarket.redeploy-deploy-publish.v1`` and
  ``onex.cmd.deploy.rebuild-requested.v1`` both at HIGH-WATERMARK **0**.

30 consumed, 30 dispatched to nowhere, offset committed every time — the OMN-16939
signature exactly: every liveness signal green, 100% of the traffic lost.

THREE DISTINCT DEFECTS ARE ASSERTED HERE, IN THE ORDER A MESSAGE HITS THEM
-------------------------------------------------------------------------
1. **Routing.** The topic must resolve to a registered dispatcher for its OWN
   ``(category, message type)``. Asserted through the REAL production helpers via
   ``omnibase_infra.validators.subscriber_dispatcher_resolution`` — not a
   re-implementation, which is how this class survived three prior gates.
2. **Handler event-type matching.** The runtime sets ``envelope.event_type`` from the
   event body, and the gate compute's published body carries the ALIAS form
   ``"omnimarket.prod-promotion-gate-evaluated"`` — no ``.v1``. The handler branched on
   ``event_type.endswith("...-evaluated.v1")``, so even a correctly-routed event would
   have fallen through to the redeploy-start branch. The pre-existing golden chain
   passed only because it fed the full topic string, a shape the bus never carries.
3. **Deploy context.** The gate hop dropped ``git_ref`` / ``build_source`` / ``scope``:
   the decision is four fields and the orchestrator is stateless, so it rebuilt a
   DEFAULTED start (``git_ref='origin/main'``, ``build_source=release``) and would have
   asked the deploy agent to rebuild the wrong tree. The gate command now carries a
   ``deploy_context`` the pure COMPUTE echoes back on the decision.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.validators.subscriber_dispatcher_resolution import scan

from omnimarket.events.runtime_deployment import (
    EnumBuildSource,
    EnumRedeployScope,
    EnumRuntimeLane,
    ModelDeployPublishCommand,
    ModelProdPromotionGateCommand,
)
from omnimarket.nodes.node_prod_promotion_gate_compute.handlers.handler_prod_promotion_gate import (
    HandlerProdPromotionGate,
)
from omnimarket.nodes.node_redeploy_orchestrator.handlers.handler_redeploy_orchestrator import (
    TOPIC_DEPLOY_PUBLISH,
    HandlerRedeployOrchestrator,
)
from omnimarket.nodes.node_redeploy_orchestrator.models.model_redeploy_start_command import (
    ModelRedeployStartCommand,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCAN_ROOT = _REPO_ROOT / "src" / "omnimarket"
_CONTRACT = _SCAN_ROOT / "nodes" / "node_redeploy_orchestrator" / "contract.yaml"

_ORCHESTRATOR = "node_redeploy_orchestrator"
_GATE_EVALUATED_TOPIC = "onex.evt.omnimarket.prod-promotion-gate-evaluated.v1"
_GRANT_RESOLVED_TOPIC = "onex.evt.omnimarket.prod-promotion-grant-resolved.v1"
_IMAGE_BUILT_TOPIC = "onex.evt.omnimarket.runtime-image-built.v1"
_START_TOPIC = "onex.cmd.omnimarket.redeploy-start.v1"

# Every topic the redeploy FSM traverses between a merge and a lane rebuild. Each one
# must resolve; the orchestrator's remaining subscribe topics (readiness-gate outcomes,
# rolled-back, runtime-booted, runtime-manifest-published) have no handler branch and
# stay on the OMN-16939 burn-down baseline — wiring them without a branch would route
# them into the redeploy-start path, and a runtime-booted event doing that is an
# unbounded redeploy loop.
_FSM_PATH_TOPICS = (
    _START_TOPIC,
    _IMAGE_BUILT_TOPIC,
    _GRANT_RESOLVED_TOPIC,
    _GATE_EVALUATED_TOPIC,
)

# A scan that collapses returns zero findings and reads exactly like a clean tree, so
# the contract count is the positive control on every assertion below.
_MIN_EXPECTED_CONTRACTS = 300


def _category_for(topic: str) -> str:
    """The category a message on ``topic`` actually arrives under."""
    return {"evt": "event", "cmd": "command", "intent": "intent"}[topic.split(".")[1]]


@pytest.fixture(scope="module")
def scan_result() -> tuple[list[object], int]:
    """One real discovery + resolution pass over the omnimarket contract tree."""
    findings, contract_count = scan(_SCAN_ROOT)
    return list(findings), contract_count


@pytest.mark.unit
def test_scan_is_not_vacuous(scan_result: tuple[list[object], int]) -> None:
    """Positive control: a collapsed discovery would make every assertion below pass."""
    _, contract_count = scan_result
    assert contract_count >= _MIN_EXPECTED_CONTRACTS, (
        f"discovery collapsed to {contract_count} contracts; every resolution "
        "assertion in this module would then be vacuously green"
    )


@pytest.mark.unit
def test_fsm_path_topics_resolve_to_a_registered_dispatcher(
    scan_result: tuple[list[object], int],
) -> None:
    """RED before the fix: all three event topics report ``category_mismatch``.

    Resolved through the real ``_topics_for_handler_entry`` /
    ``derive_entry_message_category`` / ``derive_entry_message_types`` helpers that
    ``_prepare_handler_wiring`` itself calls, so the gate cannot drift from the runtime.
    """
    findings, _ = scan_result
    unresolved = {
        (f.contract, f.topic): f.reason  # type: ignore[attr-defined]
        for f in findings
    }
    offenders = {
        topic: unresolved[(_ORCHESTRATOR, topic)]
        for topic in _FSM_PATH_TOPICS
        if (_ORCHESTRATOR, topic) in unresolved
    }
    assert not offenders, (
        f"{_ORCHESTRATOR} subscribes to these redeploy-FSM topics but no dispatcher "
        f"can ever receive them: {offenders}"
    )


@pytest.mark.unit
def test_each_fsm_path_topic_declares_its_own_category() -> None:
    """The contract-level invariant, stated where a reviewer reads it.

    An entry that omits ``message_category`` inherits the category derived from
    ``subscribe_topics[0]`` — a ``.cmd.`` topic here — so an event topic sharing that
    entry registers as ``command`` and never matches a real event.
    """
    contract = yaml.safe_load(_CONTRACT.read_text())
    subscribe_topics = contract["event_bus"]["subscribe_topics"]
    # The trap: the fallback category for any entry that omits message_category.
    assert _category_for(subscribe_topics[0]) == "command"

    by_topic = {
        entry["topic"]: entry
        for entry in contract["handler_routing"]["handlers"]
        if entry.get("topic")
    }
    for topic in _FSM_PATH_TOPICS:
        assert topic in subscribe_topics, f"{topic} is not a declared subscribe topic"
        entry = by_topic.get(topic)
        assert entry is not None, f"{topic} has no handler_routing entry naming it"
        assert entry.get("message_category") == _category_for(topic), (
            f"{topic} must declare message_category={_category_for(topic)!r}; "
            f"got {entry.get('message_category')!r}"
        )


def _gate_evaluated_envelope(
    decision_payload: dict[str, object], correlation_id: object
) -> ModelEventEnvelope[object]:
    """The envelope the runtime really builds for a gate-evaluated event.

    ``event_type`` is the ALIAS the auto-wiring consume boundary stamps from the event
    body (``handler_wiring`` prefers ``data["event_type"]``), which is what the gate
    COMPUTE's published envelope carries — no ``.v1`` suffix, no ``onex.evt.`` prefix.
    """
    return ModelEventEnvelope[object](
        payload=decision_payload,
        correlation_id=correlation_id,
        event_type="omnimarket.prod-promotion-gate-evaluated",
    )


@pytest.mark.unit
def test_wire_shaped_gate_decision_routes_to_the_deploy_publish_command() -> None:
    """RED before the fix: the alias event_type fell through to the start branch.

    The payload is the FLAT decision the pure COMPUTE actually publishes — not the
    ``{"decision": ..., "start": ...}`` wrapper the pre-existing golden chain fed it,
    which nothing on the bus produces.
    """
    correlation_id = uuid4()
    envelope = _gate_evaluated_envelope(
        {
            "allowed": True,
            "image_digest": None,
            "rollback_target": "omninode-runtime:v2.3.1",
            "reason": "dev lane is not gated; deploy may proceed",
        },
        correlation_id,
    )

    output = asyncio.run(HandlerRedeployOrchestrator().handle(envelope))

    assert [e.event_type for e in output.events] == [TOPIC_DEPLOY_PUBLISH]


@pytest.mark.unit
def test_deploy_command_carries_the_git_ref_the_merge_asked_for() -> None:
    """The whole two-hop chain, with the REAL compute in the middle.

    start -> gate-evaluate command -> HandlerProdPromotionGate -> decision ->
    orchestrator -> deploy-publish. RED before the fix: the orchestrator rebuilt a
    defaulted start, so the deploy agent would have been asked to rebuild
    ``origin/main`` from a ``release`` artifact instead of the merge commit the
    post-merge trigger published.
    """
    merge_sha = "46207e2a1c48ccc7ec8526d99360612531ec2a52"
    start = ModelRedeployStartCommand(
        correlation_id=uuid4(),
        git_ref=merge_sha,
        runtime_lane=EnumRuntimeLane.DEV,
        build_source=EnumBuildSource.WORKSPACE,
        scope=EnumRedeployScope.FULL,
        requested_by="gha/omnibase_infra/pr-3243",
    )
    orchestrator = HandlerRedeployOrchestrator()

    gate_output = asyncio.run(
        orchestrator.handle(
            ModelEventEnvelope[object](
                payload=start,
                correlation_id=start.correlation_id,
                event_type="omnimarket.redeploy-start",
            )
        )
    )
    assert [e.event_type for e in gate_output.events] == [
        "onex.cmd.omnimarket.prod-promotion-gate-evaluate.v1"
    ]
    gate_command = ModelProdPromotionGateCommand.model_validate(
        gate_output.events[0].payload
    )
    decision = asyncio.run(HandlerProdPromotionGate().handle(gate_command))
    assert decision.allowed

    deploy_output = asyncio.run(
        orchestrator.handle(
            _gate_evaluated_envelope(
                decision.model_dump(mode="json"), start.correlation_id
            )
        )
    )
    publish = ModelDeployPublishCommand.model_validate(deploy_output.events[0].payload)

    assert publish.git_ref == merge_sha
    assert publish.build_source is EnumBuildSource.WORKSPACE
    assert publish.runtime_lane is EnumRuntimeLane.DEV
    assert publish.requested_by == "gha/omnibase_infra/pr-3243"
