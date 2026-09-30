# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20154: every metered call path observes the call; routing reads the state.

Before this ticket the only record of a provider refusal was a process-local
dict that the runtime's own inference path never wrote. On 2026-09-30 the lab
dev lane took five z.ai 1302 refusals in six minutes and the runtime retried
into every one of them; ``attempt_history`` held ``failure_class: null`` on
all 214 attempts of the prior week. These tests pin the three halves of the
fix: the orchestrator emits an observation and stamps the attempt, the
in-process port delivers one, and routing reads a snapshot that fails closed
when it cannot be read.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from omnimarket.events.provider_quota import (
    EnumProviderQuotaOutcome,
    EnumProviderQuotaSource,
    ModelProviderQuotaObserved,
)
from omnimarket.inference.provider_quota_observation import (
    build_quota_observation,
    observe_failed_call,
    parse_provider_error_message,
)
from omnimarket.inference.provider_quota_state import (
    ModelProviderQuotaBlock,
    ModelProviderQuotaSnapshot,
    StaticProviderQuotaReader,
    read_provider_quota_snapshot,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_response_data import (
    ModelInferenceResponseData,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)

pytestmark = pytest.mark.unit

_ZAI_URL = "https://api.z.ai/api/coding/paas/v4/chat/completions"
_ZAI_1302_MESSAGE = (
    "provider HTTP 429 Too Many Requests for "
    f"{_ZAI_URL}; retry_after=17; "
    'response_body={"error":{"code":"1302","message":"Rate limit reached for requests"}}'
)
# The runtime bounds the body it carries, so a long Gemini body (a one-element
# JSON ARRAY) arrives truncated and is no longer JSON; shape taken from the
# lab receipt tests/fixtures/receipts/omn19004_gate_decided_then_429_6ce51f77.json.
_GEMINI_TRUNCATED_MESSAGE = (
    "provider HTTP 429 Too Many Requests for "
    "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions; "
    'response_body=[{\n  "error": {\n    "code": 429,\n    "message": "You '
    "exceeded your current quota. \\n* Quota exceeded for metric: "
    "generate_content_free_tier_requests, limit: 20, model: gemini-2.5-flash\\n"
    'Please retry in 8.338543974s.",\n    "status": "RESOURCE_EXHAUSTED",\n'
    '    "details": [\n      {\n        "@type": "type.googleapis.com/goo...[truncated]'
)


class TestTheRuntimeErrorMessageIsReadBack:
    def test_zai_status_body_and_retry_after(self) -> None:
        parsed = parse_provider_error_message(_ZAI_1302_MESSAGE)
        assert parsed.http_status == 429
        assert parsed.body == {
            "error": {"code": "1302", "message": "Rate limit reached for requests"}
        }
        assert parsed.headers == {"Retry-After": "17"}

    def test_a_truncated_gemini_array_still_classifies(self) -> None:
        now = datetime(2026, 9, 30, 13, 0, tzinfo=UTC)
        observation, verdict = observe_failed_call(
            tenant_id=None,
            endpoint_url=(
                "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
            ),
            api_key_ref="llm.gemini.api_key",
            model_name="gemini-2.5-flash",
            error_message=_GEMINI_TRUNCATED_MESSAGE,
            observed_at=now,
            latency_ms=900,
            source=EnumProviderQuotaSource.RUNTIME_ORCHESTRATOR,
        )
        assert verdict is not None
        assert verdict.provider_code == "RESOURCE_EXHAUSTED"
        assert observation is not None
        assert observation.outcome is EnumProviderQuotaOutcome.LIMIT_HIT
        assert observation.block_scope == "model"
        assert observation.blocked_until == now + timedelta(seconds=8.338543974)

    def test_a_non_http_failure_is_a_failed_call_not_a_block(self) -> None:
        observation, verdict = observe_failed_call(
            tenant_id=None,
            endpoint_url=_ZAI_URL,
            api_key_ref="llm.glm.api_key",
            model_name="glm-5.3",
            error_message="request timed out",
            observed_at=datetime.now(UTC),
            latency_ms=30000,
            source=EnumProviderQuotaSource.RUNTIME_ORCHESTRATOR,
        )
        assert verdict is None
        assert observation is not None
        assert observation.outcome is EnumProviderQuotaOutcome.CALL_FAILED
        assert observation.http_status is None


def test_an_unmetered_endpoint_is_never_observed() -> None:
    assert (
        build_quota_observation(
            tenant_id=None,
            endpoint_url="http://local.test:8000/v1/chat/completions",
            api_key_ref=None,
            model_name="qwen3.8",
            succeeded=True,
            observed_at=datetime.now(UTC),
            latency_ms=10,
            source=EnumProviderQuotaSource.INPROCESS_EFFECT,
        )
        is None
    )


class TestTheOrchestratorObservesTheCall:
    """The runtime inference path is the one that never recorded anything."""

    @staticmethod
    def _routed(correlation_id: UUID) -> HandlerDelegationWorkflow:
        handler = HandlerDelegationWorkflow(quota_reader=StaticProviderQuotaReader(()))
        handler.handle_delegation_request(
            ModelDelegationRequest(
                prompt="summarise this",
                task_type="document",  # type: ignore[arg-type]
                correlation_id=correlation_id,
                emitted_at=datetime.now(UTC),
            )
        )
        handler.handle_routing_decision(
            ModelRoutingDecision(
                correlation_id=correlation_id,
                task_type="document",
                selected_model="glm-5.3",
                selected_backend_id=uuid4(),
                selected_backend_ref="cloud-glm-5-3",
                endpoint_url=_ZAI_URL,
                api_key_ref="llm.glm.api_key",
                cost_tier="low",
                tier_name="cheap_cloud",
                max_context_tokens=128000,
                max_tokens=8192,
                system_prompt="Be brief.",
                rationale="OMN-20154 fixture.",
            )
        )
        return handler

    def test_a_1302_is_observed_as_a_model_cooldown(self) -> None:
        correlation_id = uuid4()
        handler = self._routed(correlation_id)
        events = handler.handle_inference_response(
            ModelInferenceResponseData(
                correlation_id=correlation_id,
                content="",
                model_used="glm-5.3",
                latency_ms=1200,
                error_message=_ZAI_1302_MESSAGE,
            )
        )
        observations = [e for e in events if isinstance(e, ModelProviderQuotaObserved)]
        assert len(observations) == 1
        observed = observations[0]
        assert observed.outcome is EnumProviderQuotaOutcome.LIMIT_HIT
        assert (observed.provider_id, observed.provider_code) == ("zai", "1302")
        assert observed.credential_ref == "llm.glm.api_key"
        assert observed.block_scope == "model"
        assert observed.disposition == "cooldown"
        assert observed.blocked_until == observed.observed_at + timedelta(seconds=17)
        assert observed.source is EnumProviderQuotaSource.RUNTIME_ORCHESTRATOR

    def test_the_attempt_row_carries_provider_status_and_failure_class(self) -> None:
        correlation_id = uuid4()
        handler = self._routed(correlation_id)
        handler.handle_inference_response(
            ModelInferenceResponseData(
                correlation_id=correlation_id,
                content="",
                model_used="glm-5.3",
                latency_ms=1200,
                error_message=_ZAI_1302_MESSAGE,
            )
        )
        attempt = handler.workflows[correlation_id].escalation_history[-1]
        assert attempt.provider_id == "zai"
        assert attempt.http_status == 429
        assert attempt.provider_code == "1302"
        assert attempt.failure_class == "rate_limited"

    def test_a_successful_call_is_observed_and_counted(self) -> None:
        correlation_id = uuid4()
        handler = self._routed(correlation_id)
        events = handler.handle_inference_response(
            ModelInferenceResponseData(
                correlation_id=correlation_id,
                content="a summary",
                model_used="glm-5.3",
                latency_ms=800,
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
            )
        )
        observed = [e for e in events if isinstance(e, ModelProviderQuotaObserved)]
        assert [o.outcome for o in observed] == [EnumProviderQuotaOutcome.CALL_OK]
        assert observed[0].call_started_at == observed[0].observed_at - timedelta(
            milliseconds=800
        )


class _RaisingReader:
    def read_active_blocks(
        self, *, tenant_id: UUID, as_of: datetime
    ) -> Sequence[ModelProviderQuotaBlock]:
        raise ConnectionError("projection database unreachable")


class TestRoutingReadsTheProjectionAndFailsClosed:
    def test_an_unreadable_projection_withholds_every_metered_backend(self) -> None:
        snapshot = read_provider_quota_snapshot(_RaisingReader(), tenant_id=None)
        assert snapshot.readable is False
        assert "unreachable" in snapshot.unreadable_reason
        blocked = routing.quota_blocked_backend_refs(snapshot)
        backends = routing._load_bifrost_endpoints()
        for ref, backend in backends.items():
            metered = routing.quota_block_for_backend(
                ModelProviderQuotaSnapshot.unknown(as_of=snapshot.as_of, reason="x"),
                endpoint_url=backend.endpoint_url,
                api_key_ref=backend.api_key_ref,
                model_name=backend.model_name,
            )
            assert (ref in blocked) is (metered is not None)
        local = routing.BifrostBackendRef(
            endpoint_url="http://local.test:8000/v1/chat/completions",
            model_name="qwen3.8",
            timeout_ms=60000,
            max_tokens=8192,
        )
        assert routing._backend_routable(local, quota_state=snapshot) is True, (
            "an unknown quota state withheld an unmetered rung"
        )

    def test_no_reader_at_all_is_unknown_not_a_pass(self) -> None:
        snapshot = read_provider_quota_snapshot(None, tenant_id=None)
        assert snapshot.readable is False

    def test_a_zai_cooldown_blocks_every_glm_backend_on_that_credential(self) -> None:
        now = datetime.now(UTC)
        backends = routing._load_bifrost_endpoints()
        glm_refs = {
            ref
            for ref, b in backends.items()
            if "api.z.ai" in b.endpoint_url and b.api_key_ref == "llm.glm.api_key"
        }
        assert glm_refs, "the packaged contract declares a GLM backend"
        snapshot = ModelProviderQuotaSnapshot(
            tenant_id=None,
            as_of=now,
            readable=True,
            blocks=(
                ModelProviderQuotaBlock(
                    credential_ref="llm.glm.api_key",
                    provider_id="zai",
                    model_scope="*",
                    disposition="cooldown",
                    blocked_until=now + timedelta(seconds=60),
                ),
            ),
        )
        blocked = routing.quota_blocked_backend_refs(snapshot)
        assert glm_refs <= blocked
        # Nothing on another provider is collateral.
        assert all("api.z.ai" in backends[ref].endpoint_url for ref in blocked)
        # And the block lifts at the provider's time.
        lifted = snapshot.model_copy(update={"as_of": now + timedelta(seconds=61)})
        assert routing.quota_blocked_backend_refs(lifted) == frozenset()


class TestTheInProcessPortDeliversAndRemembers:
    def test_the_port_delivers_the_effects_observation_and_sees_it(self) -> None:
        from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
            LocalDelegationDispatchPort,
        )
        from omnimarket.nodes.node_llm_delegation_call_effect import (
            ModelLlmDelegationCallResult,
        )

        delivered: list[ModelProviderQuotaObserved] = []

        class _Sink:
            def emit(self, observation: ModelProviderQuotaObserved) -> None:
                delivered.append(observation)

        port = LocalDelegationDispatchPort(
            quota_reader=StaticProviderQuotaReader(()),
            quota_observation_sink=_Sink(),
            effect_process_boundary=False,
        )
        observation, _ = observe_failed_call(
            tenant_id=None,
            endpoint_url=_ZAI_URL,
            api_key_ref="llm.glm.api_key",
            model_name="glm-5.3",
            error_message=_ZAI_1302_MESSAGE,
            observed_at=datetime.now(UTC),
            latency_ms=0,
            source=EnumProviderQuotaSource.INPROCESS_EFFECT,
        )
        assert observation is not None
        result = ModelLlmDelegationCallResult(
            request_id="r",
            success=False,
            http_status=429,
            provider_code="1302",
            quota_observation=observation,
        )
        seen: list[ModelProviderQuotaObserved] = []
        port._deliver_quota_observation(result, seen)
        assert delivered == [observation]
        assert seen == [observation]
        # The very next resolution in this dispatch sees the refusal before the
        # projection has folded it.
        snapshot = port._quota_snapshot(seen)
        assert snapshot.readable is True
        assert [b.provider_id for b in snapshot.blocks] == ["zai"]


class TestTheInProcessTerminalReachesThePlatformTable:
    """Coverage: a lane's in-process delegation used to live only on the laptop."""

    def test_the_terminal_is_published_on_the_nodes_own_topics(self) -> None:
        from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
            LocalDelegationDispatchPort,
        )

        published: list[dict[str, object]] = []

        class _Publisher:
            def publish(self, **event: object) -> bool:
                published.append(event)
                return True

        port = LocalDelegationDispatchPort(
            terminal_publisher=_Publisher(),  # type: ignore[arg-type]
            effect_process_boundary=False,
        )
        correlation = str(uuid4())
        payload: dict[str, object] = {"correlation_id": correlation, "status": "ok"}
        port._publish_terminal(payload, quality_passed=True)
        port._publish_terminal(payload, quality_passed=False)
        assert (
            [e["topic"] for e in published]
            == [
                "onex.evt.omnimarket.delegate-skill-completed.v1",  # onex-topic-allow: asserted against the contract
                "onex.evt.omnimarket.delegate-skill-failed.v1",  # onex-topic-allow: asserted against the contract
            ]
        )
        assert all(e["payload"] is payload for e in published)
        assert published[0]["event_id"] == f"delegate-skill-terminal-{correlation}"
