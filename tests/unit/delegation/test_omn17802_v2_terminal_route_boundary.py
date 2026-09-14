# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17802: the concrete v2 terminal is constructed at the route boundary.

The v1 terminal answers "what happened" with a nullable shape: a reader has to
infer from absent fields whether routing ever selected a backend, and the
``provider`` a downstream receipt reports has been reconstructed from
``endpoint_url`` because nothing on the wire carried a backend identity
(``handler_delegate_skill.py`` still falls back to the URL).

The v2 family replaces that inference with three mutually exclusive concrete
classes, released in ``omnibase_core`` by #1653. This module pins the PRODUCER
half: the orchestrator selects one of the three from the run's routing
disposition and outcome, stamps ``backend_ref`` from the backend identity the
routing authority chose and ``pricing_manifest_version`` from the manifest
pinned when that route was accepted, and emits it in the SAME fan-out list as
the unchanged v1 terminal (GD-2 Option 1).

``endpoint_url`` and ``model_used`` stay on the wire as diagnostics and are
never the source of either stamp — a URL-shaped ``backend_ref`` is refused by
the released class itself, which is the test at the bottom of
``TestTheRoutedIdentityIsNotReconstructedFromDiagnostics``.
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import pytest
import yaml
from omnibase_core.enums.enum_delegation_routing_disposition import (
    EnumDelegationRoutingDisposition,
)
from omnibase_core.enums.enum_delegation_terminal_outcome import (
    EnumDelegationTerminalOutcome,
)
from omnibase_core.enums.enum_delegation_unrouted_reason import (
    EnumDelegationUnroutedReason,
)
from omnibase_core.errors import ModelOnexError
from omnibase_core.runtime.runtime_fanout_resolver import (
    assert_published_events_injective,
)
from pydantic import ValidationError

from omnimarket.nodes.node_delegation_orchestrator.contract_topics import (
    TOPIC_ID_DELEGATION_COMPLETED_V2,
    TOPIC_ID_DELEGATION_FAILED_ROUTED_V2,
    TOPIC_ID_DELEGATION_FAILED_UNROUTED_V2,
)
from omnimarket.nodes.node_delegation_orchestrator.dispatchers.dispatcher_agent_task_lifecycle import (
    _TERMINAL_TOPICS as _LIFECYCLE_TERMINAL_TOPICS,
)
from omnimarket.nodes.node_delegation_orchestrator.dispatchers.dispatcher_delegation_workflow import (
    _INTENT_TOPICS,
)
from omnimarket.nodes.node_delegation_orchestrator.dispatchers.dispatcher_quality_gate_result import (
    _TERMINAL_TOPICS as _GATE_TERMINAL_TOPICS,
)
from omnimarket.nodes.node_delegation_orchestrator.enums import EnumDelegationState
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_result import (
    ModelDelegationCompleted,
    ModelDelegationFailed,
    ModelDelegationResult,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_terminal_v2 import (
    ModelDelegationTerminalCompletedV2,
    ModelDelegationTerminalFailedRoutedV2,
    ModelDelegationTerminalFailedUnroutedV2,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_response_data import (
    ModelInferenceResponseData,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)

pytestmark = pytest.mark.unit

_BACKEND_REF = "local-qwen3-coder-30b"
_ENDPOINT = "http://192.168.86.201:8000"  # onex-allow-internal-ip OMN-10865 reason="delegation test fixture for a local LLM endpoint"
_MODEL = "qwen3-coder-30b"
_TENANT = "omn17802-tenant"


def _contract_path() -> pathlib.Path:
    import omnimarket.nodes.node_delegation_orchestrator as node_pkg

    return pathlib.Path(node_pkg.__file__).parent / "contract.yaml"


def _contract() -> dict[str, object]:
    loaded = yaml.safe_load(_contract_path().read_text())
    assert isinstance(loaded, dict)
    return loaded


def _make_request(correlation_id: UUID) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt="Write unit tests for verify_registration.py",
        task_type="test",  # type: ignore[arg-type]
        correlation_id=correlation_id,
        emitted_at=datetime.now(UTC),
        tenant_id=_TENANT,
    )


def _make_routing_decision(
    correlation_id: UUID,
    *,
    backend_ref: str = _BACKEND_REF,
) -> ModelRoutingDecision:
    return ModelRoutingDecision(
        correlation_id=correlation_id,
        task_type="test",
        selected_model=_MODEL,
        selected_backend_id=uuid5(NAMESPACE_DNS, "omninode.ai/backends/local-qwen"),
        endpoint_url=_ENDPOINT,
        cost_tier="low",
        tier_name="free_local",
        max_context_tokens=65536,
        max_tokens=4096,
        system_prompt="You are a test generation assistant.",
        rationale="Task 'test' routed via tier 'free_local'.",
        selected_backend_ref=backend_ref,
    )


def _make_success_response(correlation_id: UUID) -> ModelInferenceResponseData:
    return ModelInferenceResponseData(
        correlation_id=correlation_id,
        content="def test_verify_registration():\n    assert True",
        model_used=_MODEL,
        llm_call_id="chatcmpl-omn17802",
        latency_ms=1200,
        prompt_tokens=1234,
        completion_tokens=567,
        total_tokens=1801,
    )


def _drive(
    correlation_id: UUID,
    *,
    gate_passed: bool,
    backend_ref: str = _BACKEND_REF,
) -> list[object]:
    """Drive the real orchestrator to a routed terminal through the real legs."""
    handler = HandlerDelegationWorkflow()
    handler.handle_delegation_request(_make_request(correlation_id))
    handler.handle_routing_decision(
        _make_routing_decision(correlation_id, backend_ref=backend_ref)
    )
    handler.handle_inference_response(_make_success_response(correlation_id))
    if not gate_passed:
        # A gate failure only terminalises once the ladder cannot escalate; a
        # routable next tier would emit an escalation instead of a terminal.
        handler.workflows[correlation_id].escalation_count = 99
    events = handler.handle_gate_result(
        ModelQualityGateResult(
            correlation_id=correlation_id,
            passed=gate_passed,
            quality_score=0.95 if gate_passed else 0.10,
            failure_reasons=() if gate_passed else ("assertions_missing",),
            fallback_recommended=False,
        )
    )
    assert handler.workflows[correlation_id].state in {
        EnumDelegationState.COMPLETED,
        EnumDelegationState.FAILED,
    }, "precondition: this shape must reach a terminal, not escalate"
    return list(events)


def _drive_routing_leg_boundary_failure(correlation_id: UUID) -> list[object]:
    """Terminalise a workflow whose ROUTING leg died before selecting a backend.

    This is the only path on which the orchestrator knows that routing produced
    no decision at all, so it is the unrouted terminal's one real producer.
    """
    from omnibase_infra.runtime.boundary_failure_terminal import (
        ModelBoundaryFailureTerminal,
    )

    from omnimarket.nodes.node_delegation_orchestrator.contract_topics import (
        TOPIC_ID_ROUTING_REQUEST,
    )

    handler = HandlerDelegationWorkflow()
    handler.handle_delegation_request(_make_request(correlation_id))
    return list(
        handler.handle_boundary_failure_terminal(
            ModelBoundaryFailureTerminal(
                correlation_id=correlation_id,
                origin_topic=TOPIC_ID_ROUTING_REQUEST,
                failure_class="ProtocolConfigurationError",
                failure_code="ONEX_CORE_041_INVALID_CONFIGURATION",
                failure_reason="routing tier configuration did not resolve",
                retryable=False,
            )
        )
    )


def _only(events: list[object], cls: type) -> object:
    matches = [event for event in events if isinstance(event, cls)]
    assert len(matches) == 1, (
        f"expected exactly one {cls.__name__} in the fan-out, got "
        f"{[type(event).__name__ for event in events]}"
    )
    return matches[0]


class TestTheRoutedTerminalNamesItsOwnDispositionAndOutcome:
    """A routed run emits the concrete class its own facts select."""

    def test_a_routed_completion_emits_the_completed_v2_class(self) -> None:
        events = _drive(uuid4(), gate_passed=True)
        terminal = _only(events, ModelDelegationTerminalCompletedV2)

        assert terminal.routing_disposition is EnumDelegationRoutingDisposition.ROUTED
        assert terminal.terminal_outcome is EnumDelegationTerminalOutcome.COMPLETED
        assert terminal.backend_ref == _BACKEND_REF
        assert terminal.pricing_manifest_version >= 1
        assert terminal.quality_passed is True

    def test_a_routed_failure_emits_the_failed_routed_v2_class(self) -> None:
        events = _drive(uuid4(), gate_passed=False)
        terminal = _only(events, ModelDelegationTerminalFailedRoutedV2)

        assert terminal.routing_disposition is EnumDelegationRoutingDisposition.ROUTED
        assert terminal.terminal_outcome is EnumDelegationTerminalOutcome.FAILED
        assert terminal.backend_ref == _BACKEND_REF
        assert terminal.pricing_manifest_version >= 1
        assert terminal.quality_passed is False
        assert terminal.terminal_failure_reason

    def test_a_routing_leg_failure_emits_the_failed_unrouted_v2_class(self) -> None:
        events = _drive_routing_leg_boundary_failure(uuid4())
        terminal = _only(events, ModelDelegationTerminalFailedUnroutedV2)

        assert terminal.routing_disposition is EnumDelegationRoutingDisposition.UNROUTED
        assert terminal.terminal_outcome is EnumDelegationTerminalOutcome.FAILED
        assert isinstance(terminal.unrouted_reason, EnumDelegationUnroutedReason)
        # The unrouted class carries no routed identity at all -- the field does
        # not exist on it, which is the structural half of "carries neither
        # routed identity nor pricing fields".
        assert not hasattr(terminal, "backend_ref")
        assert not hasattr(terminal, "pricing_manifest_version")


class TestTheV1TerminalIsUnchangedInTheSameFanout:
    """GD-2 Option 1: v2 is added beside v1, it does not replace or upcast it."""

    def test_a_completion_still_emits_exactly_one_v1_completed_terminal(self) -> None:
        events = _drive(uuid4(), gate_passed=True)
        v1 = _only(events, ModelDelegationCompleted)

        assert isinstance(v1, ModelDelegationResult)
        assert v1.endpoint_url == _ENDPOINT
        assert v1.model_used == _MODEL

    def test_a_failure_still_emits_exactly_one_v1_failed_terminal(self) -> None:
        events = _drive(uuid4(), gate_passed=False)
        assert isinstance(_only(events, ModelDelegationFailed), ModelDelegationResult)

    def test_the_v1_terminal_is_not_a_v2_instance(self) -> None:
        """No upcast: the two families share no inheritance edge."""
        events = _drive(uuid4(), gate_passed=True)
        v1 = _only(events, ModelDelegationCompleted)

        assert not isinstance(v1, ModelDelegationTerminalCompletedV2)


class TestTheRoutedIdentityIsNotReconstructedFromDiagnostics:
    """``backend_ref`` and the manifest version come from route-time identity."""

    def test_the_backend_ref_is_not_the_endpoint_url_or_the_model(self) -> None:
        events = _drive(uuid4(), gate_passed=True)
        terminal = _only(events, ModelDelegationTerminalCompletedV2)

        assert terminal.backend_ref != terminal.endpoint_url
        assert terminal.backend_ref != terminal.model_used

    def test_an_endpoint_shaped_backend_ref_is_refused_by_the_class(self) -> None:
        with pytest.raises(ValidationError, match="not a URL"):
            _completed_v2_kwargs(backend_ref=_ENDPOINT)

    def test_a_zero_pricing_manifest_version_is_refused_by_the_class(self) -> None:
        with pytest.raises(ValidationError):
            _completed_v2_kwargs(pricing_manifest_version=0)

    def test_an_absent_pricing_manifest_version_is_refused_by_the_class(self) -> None:
        with pytest.raises(ValidationError):
            _completed_v2_kwargs(pricing_manifest_version=None)


class TestARunWithNoResolvedBackendCannotConstructARoutedClass:
    """The negative the plan names: no backend identity, no routed terminal."""

    def test_a_run_whose_route_carries_no_backend_ref_emits_no_routed_v2(self) -> None:
        events = _drive(uuid4(), gate_passed=True, backend_ref="")

        routed_classes = (
            ModelDelegationTerminalCompletedV2,
            ModelDelegationTerminalFailedRoutedV2,
        )

        assert not [event for event in events if isinstance(event, routed_classes)], (
            "a run with no resolved backend identity must not claim a routed class"
        )

    def test_the_unrouted_class_refuses_a_routed_identity(self) -> None:
        with pytest.raises(ValidationError):
            ModelDelegationTerminalFailedUnroutedV2(
                **_unrouted_v2_fields(),
                backend_ref=_BACKEND_REF,  # type: ignore[call-arg]
            )


class TestTheContractDeclaresEveryV2ClassAndTopic:
    """The contract is the topic authority; the map stays injective."""

    def test_the_contract_declares_three_v2_published_events_and_topics(self) -> None:
        contract = _contract()
        published = [
            entry
            for entry in contract["published_events"]  # type: ignore[index]
            if str(entry["topic"]).endswith(".v2")
        ]
        topics = [
            topic
            for topic in contract["event_bus"]["publish_topics"]  # type: ignore[index]
            if str(topic).endswith(".v2")
        ]

        assert (len(published), len(topics)) == (3, 3)

    def test_every_declared_v2_topic_is_also_a_publish_topic(self) -> None:
        contract = _contract()
        declared = {
            str(entry["topic"])
            for entry in contract["published_events"]  # type: ignore[index]
            if str(entry["topic"]).endswith(".v2")
        }

        assert declared <= set(contract["event_bus"]["publish_topics"])  # type: ignore[index]

    def test_the_published_events_map_is_injective(self) -> None:
        contract = _contract()
        assert_published_events_injective(
            {
                str(entry["event_type"]): str(entry["topic"])
                for entry in contract["published_events"]  # type: ignore[index]
            },
            context="node_delegation_orchestrator",
        )

    def test_a_deliberately_duplicated_topic_raises_injective(self) -> None:
        """Positive control for the assertion above: it does refuse a collision."""
        contract = _contract()
        published = {
            str(entry["event_type"]): str(entry["topic"])
            for entry in contract["published_events"]  # type: ignore[index]
        }
        published["DelegationTerminalFailedUnroutedV2"] = published[
            "DelegationTerminalFailedRoutedV2"
        ]

        with pytest.raises(ModelOnexError, match="injective"):
            assert_published_events_injective(
                published, context="node_delegation_orchestrator"
            )


class TestEveryDispatcherResolvesEveryV2Class:
    """All three class-to-topic maps carry the three new classes."""

    @pytest.mark.parametrize(
        "class_to_topic",
        [_INTENT_TOPICS, _GATE_TERMINAL_TOPICS, _LIFECYCLE_TERMINAL_TOPICS],
        ids=["workflow", "quality_gate_result", "agent_task_lifecycle"],
    )
    def test_the_map_resolves_each_v2_class_to_its_contract_topic(
        self, class_to_topic: dict[type, str]
    ) -> None:
        assert class_to_topic[ModelDelegationTerminalCompletedV2] == (
            TOPIC_ID_DELEGATION_COMPLETED_V2
        )
        assert class_to_topic[ModelDelegationTerminalFailedRoutedV2] == (
            TOPIC_ID_DELEGATION_FAILED_ROUTED_V2
        )
        assert class_to_topic[ModelDelegationTerminalFailedUnroutedV2] == (
            TOPIC_ID_DELEGATION_FAILED_UNROUTED_V2
        )


class TestAnUnstatableV2TerminalIsRecordedNotDropped:
    """The one honest gap this producer has, pinned so it stays visible.

    Both released routed classes require a ``quality_bar_evaluation``. A run
    that failed BEFORE the quality gate ran has no evaluation, and inventing a
    bar to satisfy the field would put a score nobody measured on a wire whose
    whole premise is that there are no inferred values on it.

    So such a run emits the v1 terminal alone and logs a counted, attributable
    warning naming the missing fact. That is strictly better than the two
    alternatives: raising would lose the v1 terminal the caller is waiting on
    (the OMN-14600 silent-loss class), and substituting a placeholder would make
    the v2 record dishonest. The gap belongs to the released contract, not to
    this producer, and is reported as a residual on the ticket.
    """

    def test_a_pre_gate_failure_emits_v1_only_and_names_the_missing_fact(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_response_data import (
            ModelInferenceResponseData,
        )

        cid = uuid4()
        handler = HandlerDelegationWorkflow()
        handler.handle_delegation_request(_make_request(cid))
        handler.handle_routing_decision(_make_routing_decision(cid))
        handler.workflows[cid].escalation_count = 99
        with caplog.at_level("WARNING"):
            events = handler.handle_inference_response(
                ModelInferenceResponseData(
                    correlation_id=cid,
                    content="",
                    model_used=_MODEL,
                    llm_call_id="chatcmpl-omn17802-pre-gate",
                    latency_ms=900,
                    prompt_tokens=10,
                    completion_tokens=0,
                    total_tokens=10,
                    error_message="empty message content from upstream model",
                )
            )

        assert isinstance(_only(events, ModelDelegationFailed), ModelDelegationResult)
        assert not [
            event
            for event in events
            if isinstance(
                event,
                (
                    ModelDelegationTerminalCompletedV2,
                    ModelDelegationTerminalFailedRoutedV2,
                    ModelDelegationTerminalFailedUnroutedV2,
                ),
            )
        ]
        assert "delegation_terminal_v2_unrepresentable" in caplog.text
        assert "quality_bar_evaluation" in caplog.text

    def test_the_gap_log_is_a_measured_absence_not_a_parse_artifact(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Positive control: the same probe reads EMPTY on a representable run."""
        with caplog.at_level("WARNING"):
            _drive(uuid4(), gate_passed=True)

        assert "delegation_terminal_v2_unrepresentable" not in caplog.text


def _unrouted_v2_fields() -> dict[str, object]:
    return {
        "correlation_id": uuid4(),
        "task_type": "test",
        "model_used": "none",
        "endpoint_url": "none",
        "content": "",
        "latency_ms": 1,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "fallback_to_claude": False,
        "failure_reason": "routing tier configuration did not resolve",
        "tokens_to_compliance": 0,
        "compliance_attempts": 1,
        "escalation_count": 0,
        "escalation_history": (),
        "routing_tiers_hash": "a" * 64,
        "escalation_config_hash": "b" * 64,
        "attempts_count": 1,
        "cumulative_attempt_cost": 0.0,
        "cumulative_input_tokens": 0,
        "cumulative_output_tokens": 0,
        "final_attempt_cost": 0.0,
        "context_pack_hash": "",
        "cost_tier_name": "no_cost_model",
        "tenant_id": _TENANT,
        "routing_disposition": EnumDelegationRoutingDisposition.UNROUTED,
        "terminal_outcome": EnumDelegationTerminalOutcome.FAILED,
        "unrouted_reason": EnumDelegationUnroutedReason.ROUTING_CONFIGURATION_INVALID,
        "terminal_failure_reason": "ProtocolConfigurationError",
    }


def _completed_v2_kwargs(
    *,
    backend_ref: str = _BACKEND_REF,
    pricing_manifest_version: int | None = 1,
) -> ModelDelegationTerminalCompletedV2:
    from omnibase_core.enums.enum_quality_score_comparison import (
        EnumQualityScoreComparison,
    )
    from omnibase_core.models.delegation.wire.model_delegation_terminal_v2 import (
        ModelQualityBarEvaluation,
    )

    fields = _unrouted_v2_fields()
    for key in ("unrouted_reason", "terminal_failure_reason"):
        fields.pop(key)
    fields["routing_disposition"] = EnumDelegationRoutingDisposition.ROUTED
    fields["terminal_outcome"] = EnumDelegationTerminalOutcome.COMPLETED
    fields["model_used"] = _MODEL
    fields["endpoint_url"] = _ENDPOINT
    return ModelDelegationTerminalCompletedV2(
        **fields,  # type: ignore[arg-type]
        backend_ref=backend_ref,
        pricing_manifest_version=pricing_manifest_version,  # type: ignore[arg-type]
        quality_passed=True,
        quality_bar_evaluation=ModelQualityBarEvaluation(
            quality_score=0.95,
            required_quality_bar=0.8,
            score_vs_required_bar=EnumQualityScoreComparison.AT_OR_ABOVE_BAR,
        ),
        failed_acceptance_criteria=(),
    )
