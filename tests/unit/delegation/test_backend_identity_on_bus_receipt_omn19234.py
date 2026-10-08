# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The bus receipt names the backend that answered, not a hash of the model id (OMN-19234).

Deployed-lane runs d512e180, 58b88b15 and f01fefaa (2026-09-23) all carry
``backend_id: 61c35979-844a-5e90-882d-b8bbf5231b4c``, which is
``uuid5(NAMESPACE_DNS, "omninode.ai/backends/Qwen3.8-27B")``. Every backend that
serves that model id -- local-coder and local-heavy-reasoning on .201, and
local-omnipc2-chat on .202 -- hashes to the same value, so no receipt can say
which host answered.

The routing decision already carries the backend key (``selected_backend_ref``,
OMN-14402). It was dropped between the decision and the terminal: the
orchestrator's escalation attempts recorded only ``routing_decision_id`` (the
model-id hash), and the delegate-skill terminal copied that hash into the
attempt's ``backend_id``, which is the field the CLI receipt reads. The
in-process path has always put the backend key there.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import pytest

from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    _attempt_records,
)
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
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_routing_intent import (
    HandlerRoutingIntent,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)
from tests.constants import MODEL_LOCAL_201_SERVED_ID

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("stub_provider_quota_reader")]

_MODEL_ID_HASH = uuid5(
    NAMESPACE_DNS, f"omninode.ai/backends/{MODEL_LOCAL_201_SERVED_ID}"
)


def _request(task_type: str) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt="Summarize what a Kafka consumer group offset represents.",
        task_type=task_type,
        correlation_id=uuid4(),
        emitted_at=datetime.now(UTC),
    )


def _decision(cid: UUID, backend_ref: str) -> ModelRoutingDecision:
    """Two backends serving ONE model id: the decision's hash is identical."""
    return ModelRoutingDecision(
        correlation_id=cid,
        task_type="research",
        selected_model=MODEL_LOCAL_201_SERVED_ID,
        selected_backend_id=_MODEL_ID_HASH,
        endpoint_url=f"http://local.test:8000/{backend_ref}",
        cost_tier="low",
        max_context_tokens=8192,
        max_tokens=8192,
        system_prompt="You are a research assistant.",
        rationale=f"Task routed via tier 'local' to {backend_ref}.",
        tier_name="local",
        selected_backend_ref=backend_ref,
    )


def _transport_failure(cid: UUID) -> ModelInferenceResponseData:
    return ModelInferenceResponseData(
        correlation_id=cid,
        content="",
        model_used=MODEL_LOCAL_201_SERVED_ID,
        latency_ms=15,
        prompt_tokens=20,
        completion_tokens=0,
        total_tokens=20,
        error_message="Connection refused",
    )


def _recorded_attempt(backend_ref: str, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Drive one transport failure on ``backend_ref`` and return the attempt row."""
    monkeypatch.setattr(hw, "sibling_backend_available_in_tier", lambda *_a: None)
    monkeypatch.setattr(
        hw, "next_eligible_tier", lambda _current, _excluded, **_kw: None
    )
    monkeypatch.setattr(hw, "is_free_tier", lambda _tier: False)
    handler = HandlerDelegationWorkflow(workflows={})
    request = _request("research")
    cid = request.correlation_id
    handler.handle_delegation_request(request)
    handler.handle_routing_decision(_decision(cid, backend_ref))
    handler.handle_inference_response(_transport_failure(cid))
    history = handler.workflows[cid].escalation_history
    assert len(history) == 1
    return history[0]


class TestTheRoutingDecisionNamesTheBackend:
    """AC1: two backends serving one model id differ in backend identity."""

    def test_same_model_two_backends_differ_on_the_decision(
        self, frontier_unconfigured_bifrost: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
            handler_delegation_routing as reducer,
        )

        # This regression requires two backends declaring the same served id.
        backends = reducer._load_bifrost_endpoints()
        for backend_id in ("local-coder", "local-heavy-reasoning"):
            monkeypatch.setattr(
                backends[backend_id], "model_name", MODEL_LOCAL_201_SERVED_ID
            )
        routing = HandlerRoutingIntent()
        decisions = []
        for task_type in ("code_generation", "research"):
            workflow = HandlerDelegationWorkflow(workflows={})
            intents = workflow.handle_delegation_request(_request(task_type))
            decisions.append(routing.handle(intents[0]))
        coder, heavy = decisions
        # One model id, two backends: since OMN-17122 the backend UUID hashes
        # the backend reference, so it no longer collides on the model id.
        assert coder.selected_model == heavy.selected_model
        assert coder.selected_backend_id != heavy.selected_backend_id
        # The raw backend key distinguishes them too.
        assert coder.selected_backend_ref == "local-coder"
        assert heavy.selected_backend_ref == "local-heavy-reasoning"
        assert coder.selected_backend_ref != heavy.selected_backend_ref


class TestTheAttemptCarriesTheBackendKey:
    def test_escalation_attempt_records_the_decision_backend_ref(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        attempt = _recorded_attempt("local-omnipc2-chat", monkeypatch)
        assert attempt.backend_ref == "local-omnipc2-chat"
        # The model-id hash is still recorded for correlation, unchanged.
        assert attempt.routing_decision_id == _MODEL_ID_HASH

    def test_two_hosts_one_model_produce_different_attempt_identities(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        on_201 = _recorded_attempt("local-heavy-reasoning", monkeypatch)
        on_202 = _recorded_attempt("local-omnipc2-chat", monkeypatch)
        assert on_201.routing_decision_id == on_202.routing_decision_id
        assert on_201.backend_ref != on_202.backend_ref

    def test_serialized_history_carries_the_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The terminal carries ``model_dump(mode="json")`` of each attempt."""
        attempt = _recorded_attempt("local-omnipc2-chat", monkeypatch)
        assert attempt.model_dump(mode="json")["backend_ref"] == "local-omnipc2-chat"


class TestTheTerminalAttemptNamesTheBackend:
    """AC2's in-repo half: the field the receipt reads is the backend key."""

    @staticmethod
    def _history_row(**overrides: object) -> dict[str, object]:
        row: dict[str, object] = {
            "tier_name": "local",
            "model_used": MODEL_LOCAL_201_SERVED_ID,
            "quality_score": 1.0,
            "failure_reasons": [],
            "latency_ms": 900,
            "fallback_recommended": False,
            "attempted_at": "2026-09-24T18:00:00Z",
            "routing_decision_id": str(_MODEL_ID_HASH),
            "acceptance_decision": "accept",
            "acceptance_reason": "quality_bar_met",
        }
        row.update(overrides)
        return row

    def test_bus_attempt_backend_id_is_the_backend_key(self) -> None:
        records = _attempt_records(
            {
                "escalation_history": [
                    self._history_row(backend_ref="local-omnipc2-chat"),
                ]
            }
        )
        assert [r.backend_id for r in records] == ["local-omnipc2-chat"]

    def test_two_hosts_give_two_receipt_identities(self) -> None:
        on_201 = _attempt_records(
            {"escalation_history": [self._history_row(backend_ref="local-coder")]}
        )
        on_202 = _attempt_records(
            {
                "escalation_history": [
                    self._history_row(backend_ref="local-omnipc2-chat")
                ]
            }
        )
        assert on_201[0].backend_id != on_202[0].backend_id

    def test_a_row_without_the_key_keeps_the_old_reading(self) -> None:
        """A terminal from a runtime predating this change still maps."""
        records = _attempt_records({"escalation_history": [self._history_row()]})
        assert records[0].backend_id == str(_MODEL_ID_HASH)

    def test_the_backend_key_is_never_an_endpoint_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC3 (OMN-17013): identity is the contract key, never the LAN URL."""
        attempt = _recorded_attempt("local-omnipc2-chat", monkeypatch)
        records = _attempt_records(
            {"escalation_history": [attempt.model_dump(mode="json")]}
        )
        assert records[0].backend_id == "local-omnipc2-chat"
        assert "://" not in records[0].backend_id
