# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Same-tier backend fallback on the bus orchestrator FSM [OMN-14402].

There was NO same-tier fallback. If the selected local backend's endpoint
failed (a TRANSPORT/inference error, not a quality-gate rejection), the
router escalated the ENTIRE ``local`` tier straight to ``cheap_cloud`` without
ever trying a sibling local backend that also declares the task type.
``routing_tiers.yaml``'s ``local`` tier carries TWO backends for
``research`` (``local-heavy-reasoning``, ``local-ds-v4-flash``) — before this
fix, a failure on whichever one ``delta()`` picked first
(``local-heavy-reasoning``, per the OMN-14396 id-collision fix) escalated
straight past the healthy local sibling.

OMN-16442: this chain was THREE backends until ``local-reasoner`` (.201:8001)
was retired — that endpoint is the RTX 4090 slot physically removed from .201
for RMA (OMN-16407; re-probed 2026-08-28, curl exit 7 "Couldn't connect to
server"). The chain is one hop shorter but every remaining hop now reaches a
LIVE endpoint, which is the property these tests actually protect.

This is acute right now: z.ai is 429-exhausted until 2026-07-16, so a single
local-backend transport failure used to fail the delegation outright while a
healthy local sibling sat idle.

Two test tiers:

  * ``TestSiblingFallbackFsmMechanics`` — drives the FSM directly with a
    monkeypatched ``sibling_backend_available_in_tier`` so the retry/escalate
    WIRING is proven in isolation from live routing config (mirrors
    ``test_retry_local_omn14234.py``'s ``retry_local_env`` fixture style).
  * ``TestSameTierBackendFallbackRealDispatchChain`` — drives the REAL
    dispatch path (routing reducer's ``delta()`` via ``HandlerRoutingIntent``)
    against the committed ``routing_tiers.yaml`` + ``task_class_contracts.v1.
    yaml``, task_type ``research``, proving the sibling selection + the
    "no cloud call until every local sibling is exhausted" bound end to end —
    the real regression this ticket closes (memory
    feedback_real_dispatch_path_tests: handler-isolation tests pass while the
    live chain fails).

``_select_model_for_task``'s ``exclude_backend_refs`` plumbing is proven
directly (no I/O) in ``TestSelectModelForTaskExcludesBackends``, mirroring the
OMN-14396 focused-unit-test pattern.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import pytest
from omnibase_core.models.delegation.wire import ModelInferenceIntent

from omnimarket.models.delegation.llm_cost_routing.model_llm_delegation_escalation_triggered_event import (
    ModelLlmDelegationEscalationTriggeredEvent,
)
from omnimarket.nodes.node_delegation_orchestrator.enums import EnumDelegationState
from omnimarket.nodes.node_delegation_orchestrator.handlers import (
    handler_delegation_workflow as hw,
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
from omnimarket.nodes.node_delegation_orchestrator.models.model_routing_intent import (
    ModelRoutingIntent,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    BifrostBackendRef,
    _select_model_for_task,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_routing_intent import (
    HandlerRoutingIntent,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_tier_model import (
    ModelTierModel,
)
from tests.constants import MODEL_QWEN3_27B_MTP, MODEL_QWEN3_35B_A3B

# ---------------------------------------------------------------------------
# Section A — _select_model_for_task's exclude_backend_refs (no I/O)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSelectModelForTaskExcludesBackends:
    """Focused unit tests directly on the selection primitive (OMN-14396 pattern)."""

    def _models(self) -> tuple[ModelTierModel, ModelTierModel, ModelTierModel]:
        return (
            ModelTierModel(
                id=MODEL_QWEN3_35B_A3B,
                backend_ref="local-coder",
                max_context_tokens=65536,
                use_for=("code_generation",),
                fast_path_threshold_tokens=65536,
            ),
            ModelTierModel(
                id=MODEL_QWEN3_35B_A3B,
                backend_ref="local-heavy-reasoning",
                max_context_tokens=8192,
                use_for=("research", "reasoning"),
                fast_path_threshold_tokens=8192,
            ),
            ModelTierModel(
                id=MODEL_QWEN3_27B_MTP,
                backend_ref="local-reasoner",
                max_context_tokens=24576,
                use_for=("research", "reasoning"),
                fast_path_threshold_tokens=24576,
            ),
        )

    def _backends(self) -> dict[str, BifrostBackendRef]:
        return {
            "local-coder": BifrostBackendRef(
                endpoint_url="http://local.test:8000",
                model_name=MODEL_QWEN3_35B_A3B,
                timeout_ms=30000,
                max_tokens=65536,
            ),
            "local-heavy-reasoning": BifrostBackendRef(
                endpoint_url="http://local.test:8000",
                model_name=MODEL_QWEN3_35B_A3B,
                timeout_ms=30000,
                max_tokens=8192,
            ),
            "local-reasoner": BifrostBackendRef(
                endpoint_url="http://local.test:8001",
                model_name=MODEL_QWEN3_27B_MTP,
                timeout_ms=30000,
                max_tokens=24576,
            ),
        }

    def test_no_exclusion_selects_id_pinned_backend(self) -> None:
        selected = _select_model_for_task(
            self._models(),
            "research",
            estimated_tokens=25,
            bifrost_backends=self._backends(),
            contract_model_ref=MODEL_QWEN3_35B_A3B,
        )
        assert selected is not None
        assert selected.backend_ref == "local-heavy-reasoning"

    def test_excluding_pinned_backend_falls_through_to_sibling(self) -> None:
        """OMN-14402: excluding the pinned (failed) backend must land on the
        OTHER research-capable model, not silently re-select local-coder just
        because it shares the pinned id."""
        selected = _select_model_for_task(
            self._models(),
            "research",
            estimated_tokens=25,
            bifrost_backends=self._backends(),
            contract_model_ref=MODEL_QWEN3_35B_A3B,
            exclude_backend_refs=frozenset({"local-heavy-reasoning"}),
        )
        assert selected is not None
        assert selected.backend_ref == "local-reasoner"

    def test_excluding_id_match_never_falls_back_to_non_use_for_backend(
        self,
    ) -> None:
        """The 'id-matches-but-ignores-use_for' escape hatch (OMN-10942) must
        be disabled once exclusions are active — a same-tier RETRY must never
        land on a backend that does not even serve the task type."""
        models = (
            ModelTierModel(
                id=MODEL_QWEN3_35B_A3B,
                backend_ref="local-coder",
                max_context_tokens=65536,
                use_for=("code_generation",),
                fast_path_threshold_tokens=65536,
            ),
            ModelTierModel(
                id=MODEL_QWEN3_35B_A3B,
                backend_ref="local-heavy-reasoning",
                max_context_tokens=8192,
                use_for=("research",),
                fast_path_threshold_tokens=8192,
            ),
        )
        backends = {
            "local-coder": BifrostBackendRef(
                endpoint_url="http://local.test:8000",
                model_name=MODEL_QWEN3_35B_A3B,
                timeout_ms=30000,
                max_tokens=65536,
            ),
            "local-heavy-reasoning": BifrostBackendRef(
                endpoint_url="http://local.test:8000",
                model_name=MODEL_QWEN3_35B_A3B,
                timeout_ms=30000,
                max_tokens=8192,
            ),
        }
        selected = _select_model_for_task(
            models,
            "research",
            estimated_tokens=25,
            bifrost_backends=backends,
            contract_model_ref=MODEL_QWEN3_35B_A3B,
            exclude_backend_refs=frozenset({"local-heavy-reasoning"}),
        )
        # local-coder is the only remaining id match but does not serve
        # "research" -- must be None, never a silent wrong-capability fallback.
        assert selected is None

    def test_excluding_every_candidate_returns_none(self) -> None:
        selected = _select_model_for_task(
            self._models(),
            "research",
            estimated_tokens=25,
            bifrost_backends=self._backends(),
            contract_model_ref=MODEL_QWEN3_35B_A3B,
            exclude_backend_refs=frozenset({"local-heavy-reasoning", "local-reasoner"}),
        )
        assert selected is None


# ---------------------------------------------------------------------------
# Section B — FSM mechanics (monkeypatched routing authority, fast + isolated)
# ---------------------------------------------------------------------------

_LADDER_NEXT: dict[str, str] = {"local": "cheap_cloud", "cheap_cloud": "claude"}
# Deterministic, config-declared ordering (routing_tiers.yaml declaration
# order for "research"): local-heavy-reasoning is selected first (OMN-14396
# id-collision pin), then local-ds-v4-flash.
# OMN-16442: local-reasoner removed from this order — retired dead endpoint.
_LOCAL_RESEARCH_BACKEND_ORDER: tuple[str, ...] = (
    "local-heavy-reasoning",
    "local-ds-v4-flash",
)


def _fake_sibling(tier: str, task_type: str, excluded: frozenset[str]) -> str | None:
    if tier != "local" or task_type != "research":
        return None
    for backend in _LOCAL_RESEARCH_BACKEND_ORDER:
        if backend not in excluded:
            return backend
    return None


@pytest.fixture
def sibling_fallback_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the same-tier fallback WIRING from live routing config: ``local``
    declares 3 deterministic siblings for research; escalation off local lands
    on cheap_cloud. Mirrors ``test_retry_local_omn14234.py``'s ``retry_local_env``.
    """
    monkeypatch.setattr(hw, "sibling_backend_available_in_tier", _fake_sibling)
    monkeypatch.setattr(
        hw,
        "next_eligible_tier",
        lambda current, _excluded, **_kwargs: _LADDER_NEXT.get(current),
    )
    # Isolate from OMN-14234 retry-local: these tests target TRANSPORT
    # failures, not quality-gate failures, so retry-local never applies here
    # regardless of tier freeness.
    monkeypatch.setattr(hw, "is_free_tier", lambda _tier: False)


def _make_request(cid: UUID, task_type: str = "research") -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt="Summarize what a Kafka consumer group offset represents.",
        task_type=task_type,
        correlation_id=cid,
        emitted_at=datetime.now(UTC),
    )


def _make_routing_decision(
    cid: UUID,
    backend_ref: str,
    tier_name: str = "local",
    task_type: str = "research",
) -> ModelRoutingDecision:
    return ModelRoutingDecision(
        correlation_id=cid,
        task_type=task_type,
        selected_model=MODEL_QWEN3_35B_A3B,
        selected_backend_id=uuid5(NAMESPACE_DNS, f"omninode.ai/backends/{backend_ref}"),
        endpoint_url=f"http://192.168.86.201:8000/{backend_ref}",  # onex-allow-internal-ip OMN-14402 reason="FSM-mechanics test fixture, not a live target"
        cost_tier="low" if tier_name == "local" else "medium",
        max_context_tokens=8192,
        max_tokens=8192,
        system_prompt="You are a code research assistant.",
        rationale=f"Task routed via tier '{tier_name}' to {backend_ref}.",
        tier_name=tier_name,
        selected_backend_ref=backend_ref,
    )


def _error_response(
    cid: UUID, error_message: str = "Connection refused"
) -> ModelInferenceResponseData:
    return ModelInferenceResponseData(
        correlation_id=cid,
        content="",
        model_used=MODEL_QWEN3_35B_A3B,
        latency_ms=15,
        prompt_tokens=20,
        completion_tokens=0,
        total_tokens=20,
        error_message=error_message,
    )


def _routing_intents(events: list[Any]) -> list[ModelRoutingIntent]:
    return [e for e in events if isinstance(e, ModelRoutingIntent)]


def _escalation_events(
    events: list[Any],
) -> list[ModelLlmDelegationEscalationTriggeredEvent]:
    return [
        e for e in events if isinstance(e, ModelLlmDelegationEscalationTriggeredEvent)
    ]


@pytest.mark.unit
class TestSiblingFallbackFsmMechanics:
    def test_transport_failure_retries_sibling_before_escalating(
        self, sibling_fallback_env: None
    ) -> None:
        """The headline: a transport failure on the primary local backend must
        re-route to the SAME tier excluding that backend -- no escalation
        event, no escalation_count bump, no cloud call."""
        handler = HandlerDelegationWorkflow(workflows={})
        cid = uuid4()
        handler.handle_delegation_request(_make_request(cid))
        handler.handle_routing_decision(
            _make_routing_decision(cid, "local-heavy-reasoning")
        )

        events = handler.handle_inference_response(_error_response(cid))

        routing = _routing_intents(events)
        assert len(routing) == 1
        assert routing[0].min_tier_name == "local", (
            "a same-tier backend fallback must stay on 'local', not escalate"
        )
        assert routing[0].excluded_backend_refs == ("local-heavy-reasoning",)
        assert _escalation_events(events) == [], (
            "a same-tier backend swap is not a tier escalation -- must emit "
            "no ModelLlmDelegationEscalationTriggeredEvent"
        )
        wf = handler.workflows[cid]
        assert wf.state == EnumDelegationState.ROUTED
        assert wf.escalation_count == 0
        assert wf.same_tier_failed_backend_refs == ("local-heavy-reasoning",)
        assert wf.same_tier_failed_backend_tier == "local"
        # The failed attempt is still recorded for audit even though it didn't
        # cause a real escalation.
        assert len(wf.escalation_history) == 1
        assert wf.escalation_history[0].tier_name == "local"

    def test_second_sibling_failure_retries_third_backend(
        self, sibling_fallback_env: None
    ) -> None:
        handler = HandlerDelegationWorkflow(workflows={})
        cid = uuid4()
        handler.handle_delegation_request(_make_request(cid))
        handler.handle_routing_decision(
            _make_routing_decision(cid, "local-heavy-reasoning")
        )
        handler.handle_inference_response(_error_response(cid))

        # Re-route landed on the (real routing-reducer-selected) sibling --
        # simulate ITS failure too.
        handler.handle_routing_decision(_make_routing_decision(cid, "local-reasoner"))
        events = handler.handle_inference_response(_error_response(cid))

        routing = _routing_intents(events)
        assert routing[0].min_tier_name == "local"
        assert routing[0].excluded_backend_refs == (
            "local-heavy-reasoning",
            "local-reasoner",
        )
        wf = handler.workflows[cid]
        assert wf.escalation_count == 0
        assert wf.same_tier_failed_backend_refs == (
            "local-heavy-reasoning",
            "local-reasoner",
        )

    def test_all_siblings_exhausted_then_escalates_to_cloud(
        self, sibling_fallback_env: None
    ) -> None:
        """Bounded fallback: once every eligible local backend has failed, THEN
        (and only then) escalate off the tier -- exactly the ORIGINAL behavior,
        now reached after trying every sibling instead of after one failure."""
        handler = HandlerDelegationWorkflow(workflows={})
        cid = uuid4()
        handler.handle_delegation_request(_make_request(cid))

        handler.handle_routing_decision(
            _make_routing_decision(cid, "local-heavy-reasoning")
        )
        handler.handle_inference_response(_error_response(cid))
        handler.handle_routing_decision(_make_routing_decision(cid, "local-reasoner"))
        handler.handle_inference_response(_error_response(cid))
        handler.handle_routing_decision(
            _make_routing_decision(cid, "local-ds-v4-flash")
        )

        events = handler.handle_inference_response(_error_response(cid))

        routing = _routing_intents(events)
        assert len(routing) == 1
        assert routing[0].min_tier_name == "cheap_cloud", (
            "every local sibling failed -- NOW it must escalate off the tier"
        )
        assert routing[0].excluded_backend_refs == (
            "local-ds-v4-flash",
            "local-heavy-reasoning",
            "local-reasoner",
        ), "a cross-tier re-route must not forget transport-failed backends"
        assert len(_escalation_events(events)) == 1, (
            "the real cross-tier escalation must still emit its typed event"
        )
        wf = handler.workflows[cid]
        assert wf.escalation_count == 1
        assert wf.state == EnumDelegationState.ROUTED
        local_attempts = [a for a in wf.escalation_history if a.tier_name == "local"]
        assert len(local_attempts) == 3, (
            "all 3 failed local attempts must be recorded, not just the last"
        )

    def test_cross_tier_exclusions_accumulate_for_the_whole_workflow(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A backend failed on tier N stays excluded after a distinct tier N+1.

        This guards the production shape where cheap_cloud and the claude ceiling
        can name the same backend with cheap_frontier between them.  Per-tier-only
        memory would retry exhausted provider quota under a different tier label.
        """
        observed_exclusions: list[frozenset[str]] = []

        def _next_tier(
            current: str,
            _excluded_tiers: frozenset[str],
            *,
            excluded_backend_refs: frozenset[str] = frozenset(),
            **_kwargs: Any,
        ) -> str | None:
            observed_exclusions.append(excluded_backend_refs)
            return _LADDER_NEXT.get(current)

        monkeypatch.setattr(hw, "next_eligible_tier", _next_tier)
        monkeypatch.setattr(
            hw,
            "sibling_backend_available_in_tier",
            lambda _tier, _task_type, _excluded: None,
        )

        handler = HandlerDelegationWorkflow(workflows={})
        cid = uuid4()
        handler.handle_delegation_request(_make_request(cid))
        handler.handle_routing_decision(
            _make_routing_decision(cid, "shared-provider", tier_name="local")
        )

        first_events = handler.handle_inference_response(_error_response(cid))
        first_intent = _routing_intents(first_events)[0]
        assert first_intent.excluded_backend_refs == ("shared-provider",)

        handler.handle_routing_decision(
            _make_routing_decision(cid, "distinct-provider", tier_name="cheap_cloud")
        )
        second_events = handler.handle_inference_response(_error_response(cid))
        second_intent = _routing_intents(second_events)[0]

        assert second_intent.excluded_backend_refs == (
            "distinct-provider",
            "shared-provider",
        )
        assert observed_exclusions == [
            frozenset({"shared-provider"}),
            frozenset({"distinct-provider", "shared-provider"}),
        ]
        assert handler.workflows[cid].transport_failed_backend_refs == (
            "shared-provider",
            "distinct-provider",
        )

    def test_deterministic_ordering_stable_across_runs(
        self, sibling_fallback_env: None
    ) -> None:
        """The sibling chosen on retry is stable across independent runs of the
        SAME failure sequence -- never dict/set iteration order (OMN-14401 class)."""

        def _run() -> tuple[str, ...]:
            handler = HandlerDelegationWorkflow(workflows={})
            cid = uuid4()
            handler.handle_delegation_request(_make_request(cid))
            handler.handle_routing_decision(
                _make_routing_decision(cid, "local-heavy-reasoning")
            )
            events = handler.handle_inference_response(_error_response(cid))
            return _routing_intents(events)[0].excluded_backend_refs

        results = {_run() for _ in range(5)}
        assert results == {("local-heavy-reasoning",)}, (
            "the exclusion set (and therefore the sibling delta() will select "
            "next) must be identical across independent runs"
        )

    def test_quality_gate_failure_still_uses_omn14234_retry_local_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression guard: a QUALITY-GATE rejection (not a transport failure)
        must stay on the pre-existing OMN-14234 retry-local path -- this
        ticket's transport-failure fallback must never be consulted for it."""
        monkeypatch.setattr(hw, "is_free_tier", lambda tier: tier == "local")
        monkeypatch.setattr(
            hw, "tier_max_retries", lambda tier: 2 if tier == "local" else 0
        )
        monkeypatch.setattr(
            hw,
            "next_eligible_tier",
            lambda current, _excluded, **_kwargs: _LADDER_NEXT.get(current),
        )
        sentinel_calls: list[tuple[str, str, frozenset[str]]] = []

        def _tracking_sibling(
            tier: str, task_type: str, excluded: frozenset[str]
        ) -> str | None:
            sentinel_calls.append((tier, task_type, excluded))
            return None

        monkeypatch.setattr(hw, "sibling_backend_available_in_tier", _tracking_sibling)

        handler = HandlerDelegationWorkflow(workflows={})
        cid = uuid4()
        handler.handle_delegation_request(
            _make_request(cid, task_type="code_generation")
        )
        handler.handle_routing_decision(
            _make_routing_decision(cid, "local-coder", task_type="code_generation")
        )
        handler.handle_inference_response(
            ModelInferenceResponseData(
                correlation_id=cid,
                content="def f() -> int:\n    return 1",
                model_used="qwen-coder",
                latency_ms=42,
                prompt_tokens=100,
                completion_tokens=200,
                total_tokens=300,
            )
        )

        gate_result = ModelQualityGateResult(
            correlation_id=cid,
            passed=False,
            quality_score=0.5,
            failure_reasons=("score_below_required_bar",),
            fallback_recommended=True,
        )
        intents = handler.handle_gate_result(gate_result)

        routing = _routing_intents(intents)
        assert len(routing) == 1
        assert routing[0].min_tier_name == "local"
        wf = handler.workflows[cid]
        assert wf.local_retry_count == 1, "OMN-14234 retry-local must still fire"
        assert wf.same_tier_failed_backend_refs == (), (
            "the transport-failure fallback state must be untouched by a "
            "quality-gate failure"
        )
        assert sentinel_calls == [], (
            "sibling_backend_available_in_tier must never be consulted from "
            "handle_gate_result -- that is exclusively the "
            "handle_inference_response (transport-failure) path"
        )


# ---------------------------------------------------------------------------
# Section C — real dispatch chain (routing reducer's delta(), no monkeypatch)
# ---------------------------------------------------------------------------


def _real_request(task_type: str = "research") -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt="Summarize what a Kafka consumer group offset represents.",
        task_type=task_type,
        correlation_id=uuid4(),
        emitted_at=datetime.now(UTC),
    )


@pytest.mark.unit
class TestSameTierBackendFallbackRealDispatchChain:
    """Drives the REAL routing reducer (delta() via HandlerRoutingIntent)
    against the committed routing_tiers.yaml + task_class_contracts.v1.yaml —
    the live regression this ticket closes, not handler isolation (memory
    feedback_real_dispatch_path_tests).
    """

    def test_transport_failure_falls_back_to_local_sibling_not_cloud(
        self, frontier_unconfigured_bifrost: None
    ) -> None:
        workflow = HandlerDelegationWorkflow(workflows={})
        routing_handler = HandlerRoutingIntent()
        request = _real_request()
        cid = request.correlation_id

        # Hop 1-2: orchestrator -> routing reducer -> initial decision.
        routing_intents = workflow.handle_delegation_request(request)
        decision = routing_handler.handle(routing_intents[0])
        assert decision.tier_name == "local"
        # OMN-14396 id-collision pin: research's task_model_overrides resolves
        # "Qwen3.6-35B-A3B" to local-heavy-reasoning (declares research),
        # never local-coder (shares the id, does not declare research).
        assert decision.selected_backend_ref == "local-heavy-reasoning"

        inference_intents = workflow.handle_routing_decision(decision)
        assert isinstance(inference_intents[0], ModelInferenceIntent)

        # Hop 3: the selected backend's endpoint fails (transport error) --
        # BEFORE this fix this escalated straight to cheap_cloud.
        retry_events = workflow.handle_inference_response(
            _error_response(cid, "Connection refused")
        )
        retry_intents = _routing_intents(retry_events)
        assert len(retry_intents) == 1
        assert retry_intents[0].min_tier_name == "local", (
            "must retry the SAME tier, not escalate to cheap_cloud on the "
            "FIRST local backend's transport failure"
        )
        assert retry_intents[0].excluded_backend_refs == ("local-heavy-reasoning",)
        assert _escalation_events(retry_events) == []
        assert workflow.workflows[cid].escalation_count == 0

        # Hop 4: re-resolve through the REAL routing reducer with the
        # exclusion applied -- proves delta() actually lands on the sibling.
        sibling_decision = routing_handler.handle(retry_intents[0])
        assert sibling_decision.tier_name == "local", (
            "the sibling resolution must stay on 'local', never jump to a "
            "cloud tier -- this is the live regression proof"
        )
        # OMN-16442: was "local-reasoner" (retired, dead endpoint); the next
        # live sibling for "research" is local-ds-v4-flash (.200:8101).
        assert sibling_decision.selected_backend_ref == "local-ds-v4-flash"
        assert sibling_decision.endpoint_url != decision.endpoint_url

    def test_all_local_siblings_exhausted_then_escalates_to_cheap_cloud(
        self, frontier_unconfigured_bifrost: None
    ) -> None:
        """Bounded: only after EVERY local backend serving 'research' has
        failed does the workflow reach cheap_cloud."""
        workflow = HandlerDelegationWorkflow(workflows={})
        routing_handler = HandlerRoutingIntent()
        request = _real_request()
        cid = request.correlation_id

        routing_intents = workflow.handle_delegation_request(request)
        decision = routing_handler.handle(routing_intents[0])
        assert decision.selected_backend_ref == "local-heavy-reasoning"
        workflow.handle_routing_decision(decision)

        # Failure 1: local-heavy-reasoning -> retry excludes it, lands on
        # local-ds-v4-flash.
        # OMN-16442: this used to land on local-reasoner first; that backend
        # was retired with the removed .201 GPU1, so local-ds-v4-flash is now
        # the second and LAST local rung for "research".
        events = workflow.handle_inference_response(
            _error_response(cid, "Connection refused")
        )
        retry_1 = _routing_intents(events)[0]
        assert retry_1.min_tier_name == "local"
        decision_2 = routing_handler.handle(retry_1)
        assert decision_2.tier_name == "local"
        assert decision_2.selected_backend_ref == "local-ds-v4-flash"
        workflow.handle_routing_decision(decision_2)

        # Failure 2: local-ds-v4-flash -- every local sibling for "research"
        # has now failed. THIS is where cross-tier escalation must fire.
        events = workflow.handle_inference_response(
            _error_response(cid, "Connection refused")
        )
        final_routing = _routing_intents(events)
        assert len(final_routing) == 1
        assert final_routing[0].min_tier_name == "cheap_cloud", (
            "only after all local siblings are exhausted must it escalate"
        )
        assert len(_escalation_events(events)) == 1
        assert workflow.workflows[cid].escalation_count == 1

        # Prove the escalated decision actually resolves to a real cloud
        # endpoint (not stranded) -- the never-cut proof for this ladder leg.
        cloud_decision = routing_handler.handle(final_routing[0])
        assert cloud_decision.tier_name == "cheap_cloud"
        assert cloud_decision.endpoint_url.startswith("https://")
