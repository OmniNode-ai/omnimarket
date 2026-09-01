# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17445 — the other two delegation legs must terminalize too.

OMN-17397 closed the ROUTING leg: the reducer declares a failure terminal, the
OMN-16812 consume boundary publishes onto it, and the orchestrator turns it into
``delegation-failed.v1``. It closed exactly one third of the chain. The boundary
emitter's first gate is::

    if len(failure_terminal_topics) != 1:
        return

and ``failure_terminal_topics`` is read from the failing contract's OWN declared
terminals. ``node_llm_delegation_call_effect`` and
``node_delegation_quality_gate_reducer`` declared none, so that gate resolved
ZERO and the emitter was inert for both: a dispatch failure on either leg was
DLQ'd safely, produced no terminal anywhere, and left the caller's
``gateway_workflows`` row at ``status='published' / completed_at=NULL`` — the
exact stall OMN-17397 was opened on, reachable through two doors it never shut.

WHY THIS IS NOT A TWO-LINE CONTRACT EDIT. ``_declared_failure_terminal_topics``
is read at TWO call sites in ``handler_wiring.py``, not one: the consume
boundary's emitter, and ``DispatchResultApplier(failure_terminal_topics=...)``
— the OMN-15468 AC2 guard that re-routes a RETURNED model stating a failure
verdict off the contract's success terminal. Declaring a failure terminal arms
both. The live escalation ladder depends on an ``error_message``-bearing
``ModelInferenceResponseData`` continuing to arrive on
``inference-response.v1``; if the guard read that as a failure verdict and
re-routed it, the ladder would go dark — strictly worse than the stall.

Band A settles that hazard rather than assuming either answer, and pins the
answer mechanically so a later field addition cannot silently re-open it.

Five bands:

A. **The seam (AC2).** What the OMN-15468 guard actually does to each model
   these two contracts return, once a failure terminal is declared.
B. **Declaration (AC1).** Both contracts declare exactly ONE failure terminal,
   read through ``_declared_failure_terminal_topics`` — the emitter's own
   reader, never a transcription — and the orchestrator gives each an addressee.
C. **The real dispatch path (AC3).** Per leg, through the async ``handle()``
   the live dispatcher calls, including the cross-leg refusals: a terminal that
   does not describe what the workflow is waiting on is not a verdict about it.
D. **The whole chain, real wiring (AC1/AC3).** ``wire_from_manifest`` with a
   real engine and bus, a REAL ``ProtocolConfigurationError`` raised by the REAL
   quality-gate handler and a REAL dispatch failure on the inference effect, so
   the terminal fed to the orchestrator is the one the boundary published.
E. **The ladder is intact (AC4).** The REAL inference handler, failing for real
   against an unroutable endpoint, still publishes its ``error_message``
   response to ``inference-response.v1`` through real wiring — and the
   orchestrator still escalates on it.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import pytest
import yaml
from omnibase_core.models.delegation.wire import (
    ModelInferenceResponseData,
)
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import (
    _declared_failure_terminal_topics,
    _select_dispatch_result_output_topic,
    wire_from_manifest,
)
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.boundary_failure_terminal import (
    ModelBoundaryFailureTerminal,
)
from omnibase_infra.runtime.contract_terminal_events import (
    apply_failure_terminal_guard,
    resolve_terminal_verdict,
)
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

from omnimarket.nodes.node_delegation_orchestrator.enums import EnumDelegationState
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    _DECLARED_TRANSITIONS,
    _PER_STEP_DISPATCH,
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_result import (
    ModelDelegationResult,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_llm_delegation_call_result import (
    ModelLlmDelegationCallResult,
)

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_NODES = _REPO_ROOT / "src" / "omnimarket" / "nodes"
_ORCHESTRATOR_CONTRACT = _NODES / "node_delegation_orchestrator" / "contract.yaml"
_CALL_EFFECT_CONTRACT = _NODES / "node_llm_delegation_call_effect" / "contract.yaml"
_GATE_CONTRACT = _NODES / "node_delegation_quality_gate_reducer" / "contract.yaml"

# The three command topics whose consume boundaries can fail a delegation leg.
# ``ModelBoundaryFailureTerminal.origin_topic`` carries whichever one failed —
# stamped by the boundary from the topic it was consuming, not by any caller —
# so it is the only un-forgeable way to tell the legs apart on one payload type.
_ROUTING_REQUEST_TOPIC = "onex.cmd.omnibase-infra.delegation-routing-request.v1"  # onex-topic-allow: the leg address the boundary stamps
_INFERENCE_REQUEST_TOPIC = "onex.cmd.omnibase-infra.delegation-inference-request.v1"  # onex-topic-allow: the leg address the boundary stamps
_GATE_REQUEST_TOPIC = "onex.cmd.omnibase-infra.delegation-quality-gate-request.v1"  # onex-topic-allow: the leg address the boundary stamps

_INFERENCE_RESPONSE_TOPIC = "onex.evt.omnibase-infra.inference-response.v1"  # onex-topic-allow: the success channel the escalation ladder rides
_INFERENCE_FAILED_TOPIC = "onex.evt.omnibase-infra.inference-response-failed.v1"  # onex-topic-allow: the failure terminal this ticket declares
_GATE_RESULT_TOPIC = "onex.evt.omnibase-infra.quality-gate-result.v1"  # onex-topic-allow: the gate's success terminal
_GATE_FAILED_TOPIC = "onex.evt.omnibase-infra.quality-gate-result-failed.v1"  # onex-topic-allow: the failure terminal this ticket declares

_LIVE_FAILURE_CLASS = "ProtocolConfigurationError"
_LIVE_ONEX_CODE = "ONEX_CORE_041_INVALID_CONFIGURATION"


def _raw(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), f"{path} is not a YAML mapping"
    return loaded


def _make_request(correlation_id: UUID) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt="Reply with the single word: alive.",
        task_type="research",  # type: ignore[arg-type]
        correlation_id=correlation_id,
        emitted_at=datetime.now(UTC),
    )


def _make_decision(correlation_id: UUID) -> ModelRoutingDecision:
    return ModelRoutingDecision(
        correlation_id=correlation_id,
        task_type="research",
        selected_model="local/routing-test-model",
        selected_backend_id=uuid5(NAMESPACE_DNS, "omninode.ai/backends/local-coder"),
        endpoint_url="http://test-coder:8000",
        cost_tier="low",
        tier_name="free_local",
        max_context_tokens=65536,
        max_tokens=32,
        system_prompt="You are a test assistant.",
        rationale="Task 'research' routed via tier 'free_local'.",
    )


def _make_inference_response(
    correlation_id: UUID,
    *,
    error_message: str = "",
) -> ModelInferenceResponseData:
    return ModelInferenceResponseData(
        correlation_id=correlation_id,
        content="" if error_message else "alive",
        model_used="local/routing-test-model",
        latency_ms=42,
        prompt_tokens=100,
        completion_tokens=200,
        total_tokens=300,
        error_message=error_message,
    )


def _boundary_terminal(
    correlation_id: UUID,
    *,
    origin_topic: str,
    failure_class: str = _LIVE_FAILURE_CLASS,
    failure_code: str | None = _LIVE_ONEX_CODE,
) -> ModelBoundaryFailureTerminal:
    return ModelBoundaryFailureTerminal(
        correlation_id=correlation_id,
        failure_class=failure_class,
        failure_code=failure_code,
        retryable=False,
        failure_reason=(
            f"{failure_class}: [{failure_code}] the leg's consume boundary failed "
            "the record for good"
        ),
        origin_topic=origin_topic,
    )


async def _drive_to_inference_completed(
    handler: HandlerDelegationWorkflow,
    correlation_id: UUID,
) -> None:
    """Advance a workflow to the state a quality-gate intent is emitted from."""
    await handler.handle(_make_request(correlation_id))
    await handler.handle(_make_decision(correlation_id))
    await handler.handle(_make_inference_response(correlation_id))
    assert (
        handler.workflows[correlation_id].state
        == EnumDelegationState.INFERENCE_COMPLETED
    ), "precondition: the quality-gate intent is emitted on entry to this state"


# ---------------------------------------------------------------------------
# A — the seam: what arming the OMN-15468 guard actually does (AC2)
# ---------------------------------------------------------------------------


def test_an_error_message_inference_response_states_no_failure_verdict() -> None:
    """AC2, the whole hazard, settled at the line that decides it.

    ``apply_failure_terminal_guard`` re-routes a return value only when
    ``resolve_terminal_verdict`` answers ``False``. That function reads exactly
    five fields — ``terminal_failure_cause``, ``status``, ``ok``, ``success``,
    ``contract_passed`` — and ``ModelInferenceResponseData`` (``extra='forbid'``,
    frozen) declares NONE of them. ``error_message`` is not among them and is not
    a verdict field.

    So the answer to AC2 is: the hazard is NOT real for the ladder, and this is
    the assertion that keeps it that way. A future field named ``success`` or
    ``status`` on this model would flip the verdict to ``False`` and silently
    re-route every failed inference off the channel the orchestrator escalates
    from — this test fails first instead.
    """
    response = _make_inference_response(uuid4(), error_message="connect timeout")
    assert response.error_message
    assert resolve_terminal_verdict(response) is None, (
        "a verdict of False here would re-route every error_message inference "
        "response off inference-response.v1 and kill the escalation ladder"
    )


def test_the_guard_leaves_a_failed_inference_response_on_its_success_channel() -> None:
    """The behavioural half of AC2: the guard is armed, and it does not fire.

    Asserted through the guard itself with the failure terminal this ticket
    declares already in hand, so it is the post-fix configuration under test —
    not a pre-fix state that the fix could invalidate.
    """
    response = _make_inference_response(uuid4(), error_message="connect timeout")
    assert (
        apply_failure_terminal_guard(
            response,
            _INFERENCE_RESPONSE_TOPIC,
            success_topic=_INFERENCE_RESPONSE_TOPIC,
            failure_terminal_topics=(_INFERENCE_FAILED_TOPIC,),
        )
        == _INFERENCE_RESPONSE_TOPIC
    )


def test_a_failed_quality_gate_result_keeps_reaching_the_gate_result_topic() -> None:
    """``passed=False`` is a NORMAL gate outcome, not a terminal failure verdict.

    Same reader, same reason: ``passed`` (and its ``pass_`` authority sibling)
    are not among the five fields ``resolve_terminal_verdict`` consults. A gate
    result re-routed onto the failure terminal would strand every escalation
    that a failing gate is supposed to trigger.
    """
    result = ModelQualityGateResult(
        correlation_id=uuid4(),
        passed=False,
        quality_score=0.2,
        failure_reasons=("WEAK_OUTPUT: too short",),
        fallback_recommended=True,
    )
    assert resolve_terminal_verdict(result) is None
    assert (
        apply_failure_terminal_guard(
            result,
            _GATE_RESULT_TOPIC,
            success_topic=_GATE_RESULT_TOPIC,
            failure_terminal_topics=(_GATE_FAILED_TOPIC,),
        )
        == _GATE_RESULT_TOPIC
    )


def test_a_call_result_stating_success_false_is_re_routed_off_the_ladder() -> None:
    """The ONE model the armed guard does move — named, not discovered later.

    ``HandlerLlmDelegationCall.handle`` returns ``ModelLlmDelegationCallResult``
    on a timeout / HTTP / invalid-JSON failure, and that class is absent from
    the contract's ``published_events`` map, so it falls back to the applier's
    ``output_topic``. On this contract that fallback is
    ``inference-response.v1`` — a channel it can never validate on, because the
    orchestrator's route there is typed to ``ModelInferenceResponseData``. It
    declares ``success``, so once a failure terminal exists the guard moves it
    to the failure terminal instead.

    That is an improvement, not a regression, and it is asserted rather than
    left to be discovered in staging: the record stops being published onto the
    escalation channel it was never readable on.
    """
    result = ModelLlmDelegationCallResult(
        request_id=str(uuid4()),
        success=False,
        error_message="read timeout",
    )
    assert resolve_terminal_verdict(result) is False
    assert (
        apply_failure_terminal_guard(
            result,
            _INFERENCE_RESPONSE_TOPIC,
            success_topic=_INFERENCE_RESPONSE_TOPIC,
            failure_terminal_topics=(_INFERENCE_FAILED_TOPIC,),
        )
        == _INFERENCE_FAILED_TOPIC
    )


# ---------------------------------------------------------------------------
# B — declaration: the emitter's own gate resolves exactly one, per contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("contract_path", "success_topic", "failure_topic"),
    [
        pytest.param(
            _CALL_EFFECT_CONTRACT,
            _INFERENCE_RESPONSE_TOPIC,
            _INFERENCE_FAILED_TOPIC,
            id="llm-delegation-call-effect",
        ),
        pytest.param(
            _GATE_CONTRACT,
            _GATE_RESULT_TOPIC,
            _GATE_FAILED_TOPIC,
            id="delegation-quality-gate-reducer",
        ),
    ],
)
def test_the_boundary_emitter_gate_resolves_exactly_one_failure_terminal(
    contract_path: Path,
    success_topic: str,
    failure_topic: str,
) -> None:
    """RED before this ticket: ``()`` — zero, so the emitter returned early.

    Asserted through ``_declared_failure_terminal_topics`` and
    ``_select_dispatch_result_output_topic``, the two functions the runtime
    itself calls, rather than by re-reading the YAML and reasoning about it. A
    transcription would pass while the emitter stayed inert — which is exactly
    how OMN-16812 shipped a fix that did not fire.

    The success-topic assertion is load-bearing and not decoration: the
    applier's fallback topic is the contract's ``terminal_event`` if publishable
    and otherwise the FIRST publish topic, and
    ``_declared_failure_terminal_topics`` filters that topic out. Appending the
    new terminal keeps the resolution at one; prepending it would make the
    failure topic the success topic and the gate would resolve one WRONG entry.
    """
    manifest = discover_contracts_from_paths([contract_path])
    assert not manifest.errors, f"contract discovery errored: {manifest.errors}"
    (contract,) = manifest.contracts

    resolved_success = _select_dispatch_result_output_topic(contract)
    assert resolved_success == success_topic, (
        "the failure terminal must be APPENDED to publish_topics — the runtime "
        "resolves the success terminal as the first publish topic, so a "
        "prepended failure terminal silently becomes the success terminal"
    )
    assert _declared_failure_terminal_topics(
        contract, success_topic=resolved_success
    ) == (failure_topic,), (
        "the OMN-16812 emitter refuses to guess an address: with zero declared "
        "failure terminals it returns before publishing anything, so a handler "
        "failure on this leg produces no terminal and the caller's gateway row "
        "stays 'published' with completed_at NULL forever"
    )


@pytest.mark.parametrize(
    ("failure_topic", "publisher_contract"),
    [
        pytest.param(_INFERENCE_FAILED_TOPIC, _CALL_EFFECT_CONTRACT, id="inference"),
        pytest.param(_GATE_FAILED_TOPIC, _GATE_CONTRACT, id="quality-gate"),
    ],
)
def test_the_orchestrator_gives_each_failure_terminal_an_addressee(
    failure_topic: str,
    publisher_contract: Path,
) -> None:
    """A terminal with a publisher and no subscriber is a second silent drop.

    That is precisely what OMN-16812 shipped for the routing leg and OMN-17397
    had to come back and fix. The entry is asserted whole — topic binding,
    handler and event_model — so a half-declaration (subscribed but unrouted)
    cannot pass as a fix.
    """
    assert failure_topic in _raw(publisher_contract)["event_bus"]["publish_topics"], (
        "precondition: the failing node must be able to publish its own terminal"
    )

    orchestrator = _raw(_ORCHESTRATOR_CONTRACT)
    assert failure_topic in orchestrator["event_bus"]["subscribe_topics"]

    entries = [
        entry
        for entry in orchestrator["handler_routing"]["handlers"]
        if entry.get("topic") == failure_topic
    ]
    assert len(entries) == 1, (
        f"expected exactly one handler_routing entry bound to {failure_topic}; "
        f"found {len(entries)}"
    )
    entry = entries[0]
    assert entry["handler"]["name"] == "HandlerDelegationWorkflow"
    assert entry["event_model"]["name"] == "ModelBoundaryFailureTerminal"
    assert (
        entry["event_model"]["module"]
        == "omnibase_infra.runtime.boundary_failure_terminal"
    ), (
        "the event_model must name the SAME class the boundary publishes — a "
        "repo-local copy of the wire shape is the drift that lets a field "
        "rename re-open this stall silently"
    )


def test_the_boundary_terminal_still_resolves_to_one_handler_method() -> None:
    """All three legs carry ONE payload class, so ``handle`` routes it once.

    ``_PER_STEP_DISPATCH`` is keyed by payload type. Three topics carrying
    ``ModelBoundaryFailureTerminal`` therefore MUST converge on a single method
    — a second copy of the three refusals would be free to disagree with the
    first about what "already answered" means, and a double terminal for one
    correlation is made of exactly that disagreement.
    """
    assert _PER_STEP_DISPATCH[ModelBoundaryFailureTerminal] == (
        "handle_boundary_failure_terminal"
    )


def test_the_fsm_declares_a_failed_edge_for_every_terminalizable_leg() -> None:
    """``_advance`` resolves every edge from the contract FSM and rejects the rest.

    Each leg fails while the workflow sits in a different state, so each needs
    its own declared ``-> FAILED`` edge or the handler cannot terminalize the
    state the failure actually happened in:

    * routing — ``RECEIVED`` (initial) and ``ROUTED`` (escalation re-route),
      both landed by OMN-17397;
    * inference — ``ROUTED``, already declared for the terminal-inference path;
    * quality gate — ``INFERENCE_COMPLETED``, which had no ``FAILED`` edge at
      all: its only declared exit was ``-> GATE_EVALUATED``, i.e. the gate
      answering. A gate that never answers had nowhere to go.
    """
    for origin in (
        EnumDelegationState.RECEIVED,
        EnumDelegationState.ROUTED,
        EnumDelegationState.INFERENCE_COMPLETED,
    ):
        assert (origin, EnumDelegationState.FAILED) in _DECLARED_TRANSITIONS, (
            f"the contract FSM declares no {origin.value} -> FAILED edge, so a "
            "boundary failure arriving in that state cannot be terminalized"
        )


# ---------------------------------------------------------------------------
# C — the real dispatch path, per leg (AC3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_inference_leg_failure_terminalizes_a_routed_workflow() -> None:
    """The inference intent is in flight and the effect's boundary gave up.

    Driven through the async ``handle()`` the live dispatcher calls, so the
    ``_PER_STEP_DISPATCH`` binding, the per-step method and the FSM edge are all
    exercised the way production exercises them.
    """
    correlation_id = uuid4()
    handler = HandlerDelegationWorkflow(workflows={})
    await handler.handle(_make_request(correlation_id))
    await handler.handle(_make_decision(correlation_id))
    workflow = handler.workflows[correlation_id]
    assert workflow.state == EnumDelegationState.ROUTED
    assert workflow.routing_decision is not None

    events = await handler.handle(
        _boundary_terminal(correlation_id, origin_topic=_INFERENCE_REQUEST_TOPIC)
    )

    assert len(events) == 1, (
        "an inference-leg boundary failure must produce exactly one delegation "
        f"terminal; got {[type(e).__name__ for e in events]}"
    )
    terminal = events[0]
    assert isinstance(terminal, ModelDelegationResult)
    assert type(terminal).__name__ == "ModelDelegationFailed"
    assert terminal.correlation_id == correlation_id
    assert _LIVE_FAILURE_CLASS in terminal.failure_reason
    assert _LIVE_ONEX_CODE in terminal.failure_reason
    assert "timeout" not in terminal.failure_reason.casefold()
    assert terminal.terminal_failure_reason is not None
    assert _LIVE_FAILURE_CLASS in terminal.terminal_failure_reason
    # The routing decision survived, so the terminal names the model and
    # endpoint the failed call was actually routed to rather than a sentinel.
    assert terminal.model_used == "local/routing-test-model"
    assert workflow.state == EnumDelegationState.FAILED


@pytest.mark.asyncio
async def test_a_gate_leg_failure_terminalizes_and_reports_the_served_tokens() -> None:
    """The inference already ran and was metered; the gate never answered.

    Reporting ``0/0/0`` here — the routing leg's honest answer, where no model
    had been chosen — would understate real served tokens on the terminal the
    savings and delegation projections are built from. The counts come from
    what the workflow recorded, so each leg reports what actually happened on
    it.
    """
    correlation_id = uuid4()
    handler = HandlerDelegationWorkflow(workflows={})
    await _drive_to_inference_completed(handler, correlation_id)
    workflow = handler.workflows[correlation_id]

    events = await handler.handle(
        _boundary_terminal(correlation_id, origin_topic=_GATE_REQUEST_TOPIC)
    )

    assert len(events) == 1
    terminal = events[0]
    assert type(terminal).__name__ == "ModelDelegationFailed"
    assert terminal.prompt_tokens == 100
    assert terminal.completion_tokens == 200
    assert terminal.total_tokens == 300
    assert terminal.quality_passed is False
    assert _LIVE_ONEX_CODE in terminal.failure_reason
    assert workflow.state == EnumDelegationState.FAILED


@pytest.mark.asyncio
async def test_the_routing_leg_answer_is_unchanged_by_the_generalization() -> None:
    """OMN-17397's incident shape must survive being generalized to three legs.

    Same correlation, same state, same zeros: no inference ran, so the terminal
    still reports no tokens, and the ``RECEIVED -> FAILED`` edge still fires.
    """
    correlation_id = uuid4()
    handler = HandlerDelegationWorkflow(workflows={})
    await handler.handle(_make_request(correlation_id))
    assert handler.workflows[correlation_id].state == EnumDelegationState.RECEIVED

    events = await handler.handle(
        _boundary_terminal(correlation_id, origin_topic=_ROUTING_REQUEST_TOPIC)
    )

    assert len(events) == 1
    terminal = events[0]
    assert type(terminal).__name__ == "ModelDelegationFailed"
    assert terminal.prompt_tokens == 0
    assert terminal.completion_tokens == 0
    assert terminal.total_tokens == 0
    assert handler.workflows[correlation_id].state == EnumDelegationState.FAILED


@pytest.mark.asyncio
async def test_a_leg_terminal_arriving_in_the_wrong_state_is_refused() -> None:
    """Refusal 3, per leg: a terminal must describe what the workflow awaits.

    An inference failure cannot be the verdict on a workflow that has not been
    routed yet, and a gate failure cannot be the verdict on one that has not
    produced a response yet. Acting on either would race the live path into a
    second terminal for one correlation.
    """
    # Inference-leg terminal while still waiting on the ROUTING decision.
    early = uuid4()
    handler = HandlerDelegationWorkflow(workflows={})
    await handler.handle(_make_request(early))
    assert (
        await handler.handle(
            _boundary_terminal(early, origin_topic=_INFERENCE_REQUEST_TOPIC)
        )
        == []
    )
    assert handler.workflows[early].state == EnumDelegationState.RECEIVED

    # Gate-leg terminal while the inference is still in flight.
    mid = uuid4()
    await handler.handle(_make_request(mid))
    await handler.handle(_make_decision(mid))
    assert (
        await handler.handle(_boundary_terminal(mid, origin_topic=_GATE_REQUEST_TOPIC))
        == []
    )
    assert handler.workflows[mid].state == EnumDelegationState.ROUTED


@pytest.mark.asyncio
async def test_a_terminal_from_an_unrecognized_leg_is_refused() -> None:
    """Fail closed on an origin this orchestrator does not dispatch to.

    ``origin_topic`` is the only thing distinguishing the legs on one payload
    type. An unknown value means the boundary answered for a leg this workflow
    has no model of, and guessing a terminalizable state for it would close a
    caller's row on the strength of a topic string nobody here understands.
    """
    correlation_id = uuid4()
    handler = HandlerDelegationWorkflow(workflows={})
    await handler.handle(_make_request(correlation_id))

    assert (
        await handler.handle(
            _boundary_terminal(
                correlation_id,
                origin_topic="onex.cmd.omnibase-infra.some-other-node.v1",  # onex-topic-allow: deliberately not a delegation leg
            )
        )
        == []
    )
    assert handler.workflows[correlation_id].state == EnumDelegationState.RECEIVED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "origin_topic",
    [_INFERENCE_REQUEST_TOPIC, _GATE_REQUEST_TOPIC],
)
async def test_a_redelivered_leg_terminal_emits_exactly_one_terminal(
    origin_topic: str,
) -> None:
    """Refusals 1 and 2 hold for the new legs: Kafka is at-least-once.

    A second ``ModelDelegationFailed`` for one correlation would double-write
    the delegation projection and re-close an already-closed gateway row.
    """
    correlation_id = uuid4()
    handler = HandlerDelegationWorkflow(workflows={})
    await _drive_to_inference_completed(handler, correlation_id)
    if origin_topic == _INFERENCE_REQUEST_TOPIC:
        # Rewind to the state the inference leg fails in, without re-deriving it.
        handler.workflows[correlation_id].state = EnumDelegationState.ROUTED

    first = await handler.handle(
        _boundary_terminal(correlation_id, origin_topic=origin_topic)
    )
    second = await handler.handle(
        _boundary_terminal(correlation_id, origin_topic=origin_topic)
    )

    assert len(first) == 1
    assert second == [], (
        "the workflow is already FAILED; a redelivered boundary terminal must "
        "be a no-op, not a second terminal event"
    )

    # Refusal 1: a correlation this orchestrator never accepted.
    assert (
        await handler.handle(_boundary_terminal(uuid4(), origin_topic=origin_topic))
        == []
    )


# ---------------------------------------------------------------------------
# D / E — the whole chain against real wiring
# ---------------------------------------------------------------------------


class _DlqRecordingInmemoryBus(EventBusInmemory):
    """In-memory bus honoring the boundary's duck-typed DLQ contract.

    ``EventBusInmemory`` has no ``_publish_raw_to_dlq``; ``EventBusKafka`` does,
    and that method is the only reason the boundary preserves the record at
    all. Without it the boundary takes the ``message_lost`` branch instead of
    the DLQ-persisted branch the terminal is emitted from.
    """

    async def _publish_raw_to_dlq(
        self,
        *,
        original_topic: str,
        raw_msg: object,
        error: Exception,
        correlation_id: UUID,
        failure_type: str,
        consumer_group: str,
        dlq_topic: str,
    ) -> bool:
        record = ModelEventEnvelope[object](
            payload={
                "original_topic": original_topic,
                "failure_type": failure_type,
                "error_type": type(error).__name__,
                "error": str(error),
            },
            correlation_id=correlation_id,
            event_type="omnibase-infra.dlq",
        )
        await self.publish(
            dlq_topic, None, record.model_dump_json().encode("utf-8"), None
        )
        return True


# A task-class contract whose ``research`` entry names a response contract that
# does not import. ``_load_response_contract_schema`` raises the REAL
# ``ProtocolConfigurationError`` for exactly this case ("a contract-authoring
# bug that must surface loudly at first use") — the same class, from the same
# family of causes, as the live OMN-17397 incident, reached through real config
# resolution rather than a stand-in raise.
_UNRESOLVABLE_TASK_CLASS_CONTRACT = """
version: "1.0.0"
task_classes:
  research:
    response_contract_ref: "omnimarket.does.not.exist.NoSuchResponseContract"
"""


@contextlib.contextmanager
def _task_class_contract_cache_cleared() -> Any:
    """Clear the process-lifetime task-class contract caches around a test.

    ``_get_task_class_contract`` and ``_load_response_contract_schema`` are both
    ``lru_cache``d for the process, so a ``TASK_CLASS_CONTRACT_PATH`` override
    that is set AFTER a first read is silently ignored — and one set before is
    silently inherited by the next test. Cleared on the way in and on the way
    out so neither direction can leak.
    """
    import omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing as routing

    routing._get_task_class_contract.cache_clear()
    routing._load_response_contract_schema.cache_clear()
    try:
        yield
    finally:
        routing._get_task_class_contract.cache_clear()
        routing._load_response_contract_schema.cache_clear()


@pytest.fixture
def _gate_raises_on_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    contract_path = tmp_path / "task_class_contracts.v1.yaml"
    contract_path.write_text(_UNRESOLVABLE_TASK_CLASS_CONTRACT, encoding="utf-8")
    monkeypatch.setenv("TASK_CLASS_CONTRACT_PATH", str(contract_path))
    # OMN-14507's boundary DLQ is default-OFF behind this flag; the staging lane
    # runs with it ON, and the terminal emission is reached from the
    # DLQ-persisted branch.
    monkeypatch.setenv("ONEX_BOUNDARY_DLQ_ENABLED", "true")
    with _task_class_contract_cache_cleared():
        yield


@pytest.fixture
def _boundary_dlq_enabled(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("ONEX_BOUNDARY_DLQ_ENABLED", "true")
    return


# The escalation ladder is contract-bounded: ``handle_inference_response``
# fails closed when the task class declares no ``escalation_policy``. The unit
# environment ships no task-class contract at all, so band E supplies the
# smallest one that makes the ladder reachable — a budget of 1 is enough to
# prove the response reached the ladder rather than being re-routed away from
# it.
_RESEARCH_TASK_CLASS_CONTRACT = """
version: "1.0.0"
task_classes:
  research:
    escalation_policy:
      max_escalations: 1
"""


@pytest.fixture
def _research_escalation_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    contract_path = tmp_path / "task_class_contracts.v1.yaml"
    contract_path.write_text(_RESEARCH_TASK_CLASS_CONTRACT, encoding="utf-8")
    monkeypatch.setenv("TASK_CLASS_CONTRACT_PATH", str(contract_path))
    monkeypatch.setenv("ONEX_BOUNDARY_DLQ_ENABLED", "true")
    with _task_class_contract_cache_cleared():
        yield


async def _run_one_record(
    *,
    contract_path: Path,
    command_topic: str,
    terminal_topic: str,
    payload: dict[str, object],
    event_type: str,
    correlation_id: UUID,
    timeout: float = 30.0,
) -> ModelEventEnvelope[object] | None:
    """Publish one record into a node's REAL wiring; return what landed on ``terminal_topic``.

    Real contract discovery, real ``wire_from_manifest``, real
    ``MessageDispatchEngine``, real handler, real consume boundary. The only
    stand-in is the bus, and only because the boundary's DLQ leg is duck-typed.
    """
    manifest = discover_contracts_from_paths([contract_path])
    assert not manifest.errors, f"contract discovery errored: {manifest.errors}"

    bus = _DlqRecordingInmemoryBus()
    await bus.start()
    seen: list[ModelEventEnvelope[object]] = []
    arrived = asyncio.Event()

    async def _collect(message: ModelEventMessage) -> None:
        envelope = ModelEventEnvelope[object].model_validate_json(message.value)
        if envelope.correlation_id == correlation_id:
            seen.append(envelope)
            arrived.set()

    await bus.subscribe(terminal_topic, group_id="omn17445", on_message=_collect)

    engine = MessageDispatchEngine()
    await wire_from_manifest(
        ModelAutoWiringManifest(contracts=tuple(manifest.contracts)),
        engine,
        event_bus=bus,
        environment="local",
    )
    engine.freeze()

    command = ModelEventEnvelope[object](
        payload=payload,
        correlation_id=correlation_id,
        event_type=event_type,
    )
    await bus.publish(
        command_topic, None, command.model_dump_json().encode("utf-8"), None
    )
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(arrived.wait(), timeout=timeout)
    return seen[0] if seen else None


def _quality_gate_intent_wire(correlation_id: UUID) -> dict[str, object]:
    """``ModelQualityGateIntent`` exactly as the orchestrator publishes it."""
    return {
        "intent": "quality_gate",
        "payload": {
            "correlation_id": str(correlation_id),
            "task_type": "research",
            "llm_response_content": "alive",
            "expected_markers": [],
            "min_response_length": 0,
        },
    }


def _inference_intent_wire(
    correlation_id: UUID,
    *,
    base_url: str,
) -> dict[str, object]:
    """``ModelInferenceIntent`` exactly as the orchestrator publishes it."""
    return {
        "intent": "llm_inference",
        "base_url": base_url,
        "model": "local/routing-test-model",
        "system_prompt": "You are a test assistant.",
        "prompt": "Reply with the single word: alive.",
        "max_tokens": 32,
        "temperature": 0.3,
        "timeout_seconds": 5.0,
        "correlation_id": str(correlation_id),
    }


@pytest.mark.asyncio
@pytest.mark.usefixtures("_gate_raises_on_config")
async def test_a_real_gate_raise_closes_the_workflow_end_to_end() -> None:
    """AC1/AC3 for the quality-gate leg — the artifact under test is the real one.

    ``HandlerQualityGateIntent.handle_async`` resolves the task-class response
    contract before it evaluates anything, and a ``response_contract_ref`` that
    does not import raises ``ProtocolConfigurationError`` from real config
    resolution. The boundary publishes the real terminal; the orchestrator
    consumes that exact wire payload (re-validated into the contract-declared
    ``event_model``, as the runtime does) and answers with the delegation
    terminal the gateway projects. No hand-built terminal anywhere on this path.
    """
    correlation_id = uuid4()
    envelope = await _run_one_record(
        contract_path=_GATE_CONTRACT,
        command_topic=_GATE_REQUEST_TOPIC,
        terminal_topic=_GATE_FAILED_TOPIC,
        payload=_quality_gate_intent_wire(correlation_id),
        event_type="omnibase-infra.delegation-quality-gate-request",
        correlation_id=correlation_id,
    )
    assert envelope is not None, (
        "the boundary published no terminal on the gate's declared failure "
        f"terminal {_GATE_FAILED_TOPIC} — this is the stall: the record is "
        "DLQ'd, the caller is never told, and the gateway row stays 'published'"
    )
    assert isinstance(envelope.payload, dict)
    terminal = ModelBoundaryFailureTerminal.model_validate(envelope.payload)
    assert terminal.failure_class == _LIVE_FAILURE_CLASS
    assert terminal.retryable is False
    assert terminal.origin_topic == _GATE_REQUEST_TOPIC

    handler = HandlerDelegationWorkflow(workflows={})
    await _drive_to_inference_completed(handler, correlation_id)
    events = await handler.handle(terminal)

    assert len(events) == 1
    assert type(events[0]).__name__ == "ModelDelegationFailed"
    assert _LIVE_FAILURE_CLASS in events[0].failure_reason
    assert handler.workflows[correlation_id].state == EnumDelegationState.FAILED


@pytest.mark.asyncio
@pytest.mark.usefixtures("_boundary_dlq_enabled")
async def test_a_real_inference_dispatch_failure_closes_the_workflow_end_to_end() -> (
    None
):
    """AC1/AC3 for the inference leg, on a failure class that node CAN produce.

    Measured while writing this, not assumed: this leg already has TWO layers
    that convert a failure into an ``error_message`` response rather than
    letting it reach the boundary.

    1. ``HandlerInferenceIntent.handle`` catches ``Exception`` unconditionally
       and returns an ``error_message`` response — that IS the escalation
       ladder, and band E proves it still works.
    2. ``handler_wiring._build_inference_intent_validation_failure_result`` maps
       a pre-handler ``ModelInferenceIntent`` validation miss to the same
       response shape, for exactly this reason.

    So the boundary terminal on this contract is a BACKSTOP, not the primary
    path — and it is still load-bearing, because layer 2 fires only when the
    payload still CLAIMS to be an inference intent
    (``_payload_claims_delegation_inference_intent``: ``intent ==
    'llm_inference'``). A producer that renames its discriminator clears
    neither layer: the matcher rejects it, no dispatcher matches, the record is
    DLQ'd as ``publisher_malformed``, and before this ticket nothing else
    happened at all. The stall is identical to OMN-17397's: workflow parked in
    ROUTED, gateway row never closed. Layer 1 is also a handler implementation
    choice, not a contract — a refactor that lets one exception through
    re-opens the stall on every path, and this terminal is what catches it.
    """
    correlation_id = uuid4()
    envelope = await _run_one_record(
        contract_path=_CALL_EFFECT_CONTRACT,
        command_topic=_INFERENCE_REQUEST_TOPIC,
        terminal_topic=_INFERENCE_FAILED_TOPIC,
        payload={
            # A renamed discriminator — the shape a producer/consumer version
            # skew actually has. Everything else is a well-formed intent.
            "intent": "llm_inference_v2_renamed",
            "base_url": "http://127.0.0.1:1/v1/chat/completions",
            "model": "local/routing-test-model",
            "system_prompt": "You are a test assistant.",
            "prompt": "Reply with the single word: alive.",
            "max_tokens": 32,
            "correlation_id": str(correlation_id),
        },
        event_type="omnibase-infra.delegation-inference-request",
        correlation_id=correlation_id,
    )
    assert envelope is not None, (
        "the boundary published no terminal on the inference effect's declared "
        f"failure terminal {_INFERENCE_FAILED_TOPIC}; the workflow stays parked "
        "in ROUTED and the caller waits out the whole ingress budget"
    )
    assert isinstance(envelope.payload, dict)
    terminal = ModelBoundaryFailureTerminal.model_validate(envelope.payload)
    assert terminal.origin_topic == _INFERENCE_REQUEST_TOPIC
    assert terminal.status == "failed"
    # Attributed, not "dispatch_timeout": the boundary names the class it
    # actually failed on and the reason names the malformed publisher.
    assert terminal.failure_class == "ValueError"
    assert "publisher_malformed" in terminal.failure_reason
    assert "timeout" not in terminal.failure_reason.casefold()

    handler = HandlerDelegationWorkflow(workflows={})
    await handler.handle(_make_request(correlation_id))
    await handler.handle(_make_decision(correlation_id))
    events = await handler.handle(terminal)

    assert len(events) == 1
    assert type(events[0]).__name__ == "ModelDelegationFailed"
    assert handler.workflows[correlation_id].state == EnumDelegationState.FAILED


@pytest.mark.asyncio
@pytest.mark.usefixtures("_research_escalation_budget")
async def test_the_escalation_ladder_still_rides_inference_response_v1() -> None:
    """AC4, against real wiring, with a REAL inference failure.

    The REAL ``HandlerInferenceIntent`` is driven against an unroutable endpoint
    (port 1 on loopback), so it fails the way it fails in production, catches
    the transport error and returns a ``ModelInferenceResponseData`` with
    ``error_message`` set. With the failure terminal now declared on this
    contract, the OMN-15468 guard is ARMED over that return value — and this
    asserts it still lands on ``inference-response.v1``, the channel the
    orchestrator escalates from.

    This is the test that would have caught the regression the ticket named. It
    is not a restatement of band A: band A asserts the decision function, this
    asserts the topic a real record actually reached through the real applier.
    """
    correlation_id = uuid4()
    envelope = await _run_one_record(
        contract_path=_CALL_EFFECT_CONTRACT,
        command_topic=_INFERENCE_REQUEST_TOPIC,
        terminal_topic=_INFERENCE_RESPONSE_TOPIC,
        payload=_inference_intent_wire(
            correlation_id,
            base_url="http://127.0.0.1:1/v1/chat/completions",
        ),
        event_type="omnibase-infra.delegation-inference-request",
        correlation_id=correlation_id,
        timeout=60.0,
    )
    assert envelope is not None, (
        "a failed inference response no longer reaches inference-response.v1 — "
        "the OMN-15468 guard re-routed it onto the failure terminal and the "
        "entire tier-escalation ladder is dark"
    )
    assert isinstance(envelope.payload, dict)
    response = ModelInferenceResponseData.model_validate(envelope.payload)
    assert response.error_message, (
        "precondition: the real handler must have failed against the "
        "unroutable endpoint"
    )

    # ...and the orchestrator still escalates on it, rather than terminalizing.
    handler = HandlerDelegationWorkflow(workflows={})
    await handler.handle(_make_request(correlation_id))
    await handler.handle(_make_decision(correlation_id))
    emitted = await handler.handle(response)
    workflow = handler.workflows[correlation_id]
    assert workflow.state is not EnumDelegationState.FAILED or emitted, (
        "an error_message response must drive the escalation ladder (escalate, "
        "retry, or emit an attributed terminal) — never be silently dropped"
    )
    assert workflow.escalation_history, (
        "the failed attempt must be banked into escalation history; an empty "
        "history means handle_inference_response never saw the response"
    )
