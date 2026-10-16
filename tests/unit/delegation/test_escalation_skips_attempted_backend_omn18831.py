# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A quality escalation never climbs to a rung that serves the backend just rejected.

The recorded run climbed ``cheap_cloud`` to ``claude`` on the same concrete
backend (``cloud-gemini-2-5-flash``) and the same model, so the fifth attempt
re-asked the question the fourth had just failed and was billed at the ceiling
slot's rate. The local dispatch port already skips an attempted backend when it
picks the next tier; the deployed workflow remembered only transport failures.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel

from omnimarket.nodes.node_delegation_orchestrator.enums import EnumDelegationState
from omnimarket.nodes.node_delegation_orchestrator.handlers import (
    handler_delegation_workflow as hw,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_result import (
    ModelDelegationFailed,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_response_data import (
    ModelInferenceResponseData,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_routing_intent import (
    ModelRoutingIntent,
)
from omnimarket.nodes.node_delegation_orchestrator.state_codec import decode, encode
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("stub_provider_quota_reader")]

_SHARED_BACKEND = "cloud-gemini-2-5-flash"

# The ladder the recorded run walked: two tiers declare the one backend.
_LADDER: dict[str, tuple[str, ...]] = {
    "cheap_cloud": (_SHARED_BACKEND,),
    "claude": (_SHARED_BACKEND,),
}


@pytest.fixture
def next_tier_calls(monkeypatch: pytest.MonkeyPatch) -> list[frozenset[str]]:
    """Replace the routing authority's probe with its declared exclusion rule."""
    calls: list[frozenset[str]] = []

    def fake_next_eligible_tier(
        current_tier_name: str,
        excluded_tiers: frozenset[str],
        *,
        excluded_backend_refs: frozenset[str] = frozenset(),
        **_kwargs: object,
    ) -> str | None:
        calls.append(excluded_backend_refs)
        names = list(_LADDER)
        for name in names[names.index(current_tier_name) + 1 :]:
            if name in excluded_tiers:
                continue
            if any(ref not in excluded_backend_refs for ref in _LADDER[name]):
                return name
        return None

    monkeypatch.setattr(hw, "next_eligible_tier", fake_next_eligible_tier)
    monkeypatch.setattr(hw, "is_free_tier", lambda _tier: False)
    monkeypatch.setattr(hw, "tier_max_retries", lambda _tier: 0)
    return calls


def _start(handler: hw.HandlerDelegationWorkflow) -> UUID:
    cid = uuid4()
    handler.handle_delegation_request(
        ModelDelegationRequest(
            prompt="Summarize the attached facts in two sentences.",
            task_type="research",
            correlation_id=cid,
            emitted_at=datetime.now(UTC),
        )
    )
    return cid


def _reject_on(
    handler: hw.HandlerDelegationWorkflow, cid: UUID, *, tier: str, backend: str
) -> list[BaseModel]:
    handler.handle_routing_decision(
        ModelRoutingDecision(
            correlation_id=cid,
            task_type="research",
            selected_model="gemini-2.5-flash",
            selected_backend_id=uuid4(),
            selected_backend_ref=backend,
            endpoint_url="http://provider.invalid:8000",
            cost_tier="low",
            max_context_tokens=65536,
            max_tokens=2048,
            system_prompt="Answer the request.",
            rationale="Controlled route for the recorded duplicate rung.",
            tier_name=tier,
        )
    )
    handler.handle_inference_response(
        ModelInferenceResponseData(
            correlation_id=cid,
            content="A short answer the gate scores below the bar.",
            model_used="gemini-2.5-flash",
            latency_ms=10,
        )
    )
    assert handler.workflows[cid].state is EnumDelegationState.INFERENCE_COMPLETED
    return handler.handle_gate_result(
        ModelQualityGateResult(
            correlation_id=cid,
            passed=False,
            quality_score=0.4,
            fail_category="fail_heuristic",
            failure_reasons=("below the bar",),
            fallback_recommended=True,
        )
    )


def test_the_rung_serving_the_rejected_backend_is_not_climbed_to(
    next_tier_calls: list[frozenset[str]],
) -> None:
    handler = hw.HandlerDelegationWorkflow(workflows={})
    cid = _start(handler)

    events = _reject_on(handler, cid, tier="cheap_cloud", backend=_SHARED_BACKEND)

    assert not any(isinstance(e, ModelRoutingIntent) for e in events)
    failed = next(e for e in events if isinstance(e, ModelDelegationFailed))
    assert failed.terminal_failure_reason
    assert _SHARED_BACKEND in next_tier_calls[-1]
    assert len(handler.workflows[cid].escalation_history) == 1


def test_a_rung_serving_a_different_backend_is_still_climbed_to(
    next_tier_calls: list[frozenset[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(_LADDER, "claude", ("a-different-backend",))
    handler = hw.HandlerDelegationWorkflow(workflows={})
    cid = _start(handler)

    events = _reject_on(handler, cid, tier="cheap_cloud", backend=_SHARED_BACKEND)

    route = next(e for e in events if isinstance(e, ModelRoutingIntent))
    assert route.min_tier_name == "claude"
    assert _SHARED_BACKEND in route.excluded_backend_refs


def test_the_rejected_backend_stays_excluded_after_a_state_roundtrip(
    next_tier_calls: list[frozenset[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(_LADDER, "claude", ("a-different-backend",))
    monkeypatch.setitem(_LADDER, "ceiling", (_SHARED_BACKEND, "last-backend"))
    handler = hw.HandlerDelegationWorkflow(workflows={})
    cid = _start(handler)
    _reject_on(handler, cid, tier="cheap_cloud", backend=_SHARED_BACKEND)
    handler.workflows[cid] = decode(encode(handler.workflows[cid]))

    events = _reject_on(handler, cid, tier="claude", backend="a-different-backend")

    route = next(e for e in events if isinstance(e, ModelRoutingIntent))
    assert route.min_tier_name == "ceiling"
    assert {_SHARED_BACKEND, "a-different-backend"} <= set(route.excluded_backend_refs)
