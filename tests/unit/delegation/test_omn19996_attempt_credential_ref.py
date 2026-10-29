# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19996 Amendment 1: each attempt row names the customer key it used.

On the deployed path the delegate-skill terminal carried no secret_ref (lab
rehearsal 2026-10-29, correlations e8bfb09d and 7165800c), so a key's
last_used_at never moved. The delegation workflow now stamps the attempt's
key reference on its escalation_history row, behind its own identity check.

Failure modes, each with a test below:
W1 a customer-key answer on its own attempt does not name the key.
W2 the name is lost when the row is serialised onto the terminal.
W3 a house-key or unresolved answer names a key anyway.
W4 a ref without the minted tenant shape is stamped.
W5 an answer with no attempt id stamps the live key.
W6 an answer while the workflow has no current attempt id stamps it.
W7 an answer for another attempt is accepted or stamps.
W8 an id-less stale answer from key X stamps the later attempt's key Y.
W9 a row without a ref grows a null secret_ref key on the wire.
W10 a stamp outlives its row and reaches the next attempt.
W11 the gate returns a ref other than the one bound to the matching attempt.
W12 a key proven by a call is lost when a compliance repair, in the same rung,
    mints a new attempt and fails before resolving a credential (Codex review
    of 840f49d8b); a repair that proves nothing must not be credited either.
W13 the binding or a proof is lost across the durable state codec.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from omnibase_core.models.delegation.wire import (
    EnumCredentialSource,
    ModelBudgetLimits,
)

from omnimarket.inference.provider_quota_state import StaticProviderQuotaReader
from omnimarket.nodes.node_delegation_orchestrator.handlers import (
    handler_delegation_workflow as workflow_module,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_escalation_attempt import (
    ModelDelegationEscalationAttempt,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_intent import (
    ModelInferenceIntent,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_response_data import (
    ModelInferenceResponseData,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)

pytestmark = pytest.mark.unit

URL = "https://openrouter.invalid/api/v1/chat/completions"
X = "cred_tenant-a_openrouter_" + "a" * 32
HOUSE = "llm.glm.api_key"
ERROR = "provider HTTP 401 Unauthorized for " + URL


def _routed(ref: str) -> tuple[HandlerDelegationWorkflow, UUID, UUID]:
    handler = HandlerDelegationWorkflow(quota_reader=StaticProviderQuotaReader(()))
    cid = uuid4()
    handler.handle_delegation_request(
        ModelDelegationRequest(
            prompt="say hello",
            task_type="document",
            correlation_id=cid,
            emitted_at=datetime.now(UTC),
            tenant_id="tenant-a",
        )
    )
    intents = handler.handle_routing_decision(
        ModelRoutingDecision(
            correlation_id=cid,
            task_type="document",
            selected_model="m-1",
            selected_backend_id=uuid4(),
            selected_backend_ref="byok-openrouter",
            endpoint_url=URL,
            api_key_ref=ref,
            cost_tier="tenant_overlay",
            tier_name="tenant_byok",
            max_context_tokens=32000,
            max_tokens=1024,
            system_prompt="Be brief.",
            rationale="OMN-19996",
        )
    )
    attempt_id = intents[0].inference_attempt_id
    assert attempt_id is not None
    return handler, cid, attempt_id


def _respond(
    handler: HandlerDelegationWorkflow,
    cid: UUID,
    *,
    attempt_id: UUID | None,
    source: EnumCredentialSource,
) -> None:
    handler.handle_inference_response(
        ModelInferenceResponseData(
            correlation_id=cid,
            content="",
            model_used="m-1",
            latency_ms=10,
            error_message=ERROR,
            inference_attempt_id=attempt_id,
            credential_source=source,
        )
    )


def _row(
    handler: HandlerDelegationWorkflow, cid: UUID
) -> ModelDelegationEscalationAttempt:
    return handler.workflows[cid].escalation_history[-1]


def test_a_customer_key_attempt_carries_its_ref() -> None:
    """A customer-key attempt stamps its own credential ref onto the escalation row."""
    handler, cid, attempt_id = _routed(X)
    _respond(
        handler, cid, attempt_id=attempt_id, source=EnumCredentialSource.CUSTOMER_KEY
    )
    assert _row(handler, cid).secret_ref == X


def test_the_ref_rides_the_terminal_history() -> None:
    """The stamped ref survives a JSON dump of the terminal escalation history."""
    handler, cid, attempt_id = _routed(X)
    _respond(
        handler, cid, attempt_id=attempt_id, source=EnumCredentialSource.CUSTOMER_KEY
    )
    history = [
        row.model_dump(mode="json") for row in handler.workflows[cid].escalation_history
    ]
    assert history[-1]["secret_ref"] == X


@pytest.mark.parametrize(
    "source",
    [EnumCredentialSource.HOUSE, EnumCredentialSource.NONE],
    ids=["house", "none"],
)
def test_a_non_customer_answer_stamps_nothing(source: EnumCredentialSource) -> None:
    """Non-customer credential sources leave the escalation row's secret_ref unset."""
    handler, cid, attempt_id = _routed(X)
    _respond(handler, cid, attempt_id=attempt_id, source=source)
    assert _row(handler, cid).secret_ref is None


def test_a_house_shaped_ref_stamps_nothing_even_as_customer_key() -> None:
    """A house-shaped ref is not stamped even when the source claims CUSTOMER_KEY."""
    handler, cid, attempt_id = _routed(HOUSE)
    _respond(
        handler, cid, attempt_id=attempt_id, source=EnumCredentialSource.CUSTOMER_KEY
    )
    assert _row(handler, cid).secret_ref is None


def test_a_response_without_an_attempt_id_stamps_nothing() -> None:
    """A response lacking an attempt id records a row but stamps no ref."""
    handler, cid, _ = _routed(X)
    _respond(handler, cid, attempt_id=None, source=EnumCredentialSource.CUSTOMER_KEY)
    assert _row(handler, cid).secret_ref is None


def test_a_workflow_without_a_current_attempt_id_stamps_nothing() -> None:
    """With no current attempt id on the workflow, the response stamps nothing."""
    handler, cid, attempt_id = _routed(X)
    handler.workflows[cid].current_inference_attempt_id = None
    _respond(
        handler, cid, attempt_id=attempt_id, source=EnumCredentialSource.CUSTOMER_KEY
    )
    assert _row(handler, cid).secret_ref is None


def test_a_different_attempt_id_is_rejected_and_stamps_nothing() -> None:
    """A mismatched attempt id is rejected and no escalation row is recorded."""
    handler, cid, _ = _routed(X)
    _respond(handler, cid, attempt_id=uuid4(), source=EnumCredentialSource.CUSTOMER_KEY)
    assert handler.workflows[cid].escalation_history == []


def test_an_idless_stale_answer_never_stamps_the_live_key() -> None:
    """An idless stale answer never stamps the live key of a later attempt."""
    handler, cid, _ = _routed(X)
    wf = handler.workflows[cid]
    y = "cred_tenant-a_openrouter_" + "b" * 32
    wf.current_inference_attempt_id = uuid4()
    wf.current_attempt_intent_ref = (wf.current_inference_attempt_id, y)
    _respond(handler, cid, attempt_id=None, source=EnumCredentialSource.CUSTOMER_KEY)
    assert _row(handler, cid).secret_ref is None


def test_a_history_row_without_a_ref_omits_the_key() -> None:
    """A row with no stamped ref omits the secret_ref key entirely from its JSON dump."""
    handler, cid, attempt_id = _routed(X)
    _respond(handler, cid, attempt_id=attempt_id, source=EnumCredentialSource.HOUSE)
    history = [
        row.model_dump(mode="json") for row in handler.workflows[cid].escalation_history
    ]
    assert "secret_ref" not in history[-1]


def test_a_stamp_is_consumed_by_its_row() -> None:
    """Once a row carries the stamp, nothing is left for a later attempt."""
    handler, cid, attempt_id = _routed(X)
    _respond(
        handler, cid, attempt_id=attempt_id, source=EnumCredentialSource.CUSTOMER_KEY
    )
    assert _row(handler, cid).secret_ref == X
    assert handler.workflows[cid].proven_attempt_refs == {}


def test_the_gate_returns_the_ref_bound_to_the_matching_attempt() -> None:
    """Two attempts bound to X and then Y each answer with their own ref."""
    gate = workflow_module._attempt_customer_key_ref
    first, second = uuid4(), uuid4()
    y = "cred_tenant-a_openrouter_" + "b" * 32

    def answer(attempt_id: UUID) -> ModelInferenceResponseData:
        return ModelInferenceResponseData(
            correlation_id=uuid4(),
            content="",
            model_used="m-1",
            latency_ms=10,
            inference_attempt_id=attempt_id,
            credential_source=EnumCredentialSource.CUSTOMER_KEY,
        )

    assert gate((first, X), first, answer(first)) == X
    assert gate((second, y), second, answer(second)) == y
    assert gate((second, y), second, answer(first)) is None
    assert gate((first, X), second, answer(first)) is None


@pytest.mark.parametrize(
    ("first_source", "repair_source", "expected"),
    [
        (EnumCredentialSource.CUSTOMER_KEY, EnumCredentialSource.NONE, X),
        (EnumCredentialSource.CUSTOMER_KEY, EnumCredentialSource.CUSTOMER_KEY, X),
        (EnumCredentialSource.HOUSE, EnumCredentialSource.NONE, None),
    ],
    ids=["key-used-before-unresolved-repair", "key-used-again", "no-customer-call"],
)
def test_a_compliance_repair_keeps_a_key_already_used(
    first_source: EnumCredentialSource,
    repair_source: EnumCredentialSource,
    expected: str | None,
) -> None:
    """A repair in the same rung neither drops a proven key nor earns one itself.

    Ported from Codex's independent review of 840f49d8b: a non-compliant answer
    on X triggers a schema repair with a fresh attempt id; the repair fails
    before any credential resolves. X was used, so the rung's row names X.
    """
    handler = HandlerDelegationWorkflow(quota_reader=StaticProviderQuotaReader(()))
    cid = uuid4()
    handler.handle_delegation_request(
        ModelDelegationRequest(
            prompt="review this change",
            task_type="document",
            correlation_id=cid,
            emitted_at=datetime.now(UTC),
            tenant_id="tenant-a",
            output_schema_key="review_output",
            compliance_budget=ModelBudgetLimits(
                max_tokens=100000, max_cost_usd=100, max_time_s=1000
            ),
        )
    )
    events = handler.handle_routing_decision(
        ModelRoutingDecision(
            correlation_id=cid,
            task_type="document",
            selected_model="m-1",
            selected_backend_id=uuid4(),
            selected_backend_ref="byok-openrouter",
            endpoint_url=URL,
            api_key_ref=X,
            cost_tier="tenant_overlay",
            tier_name="tenant_byok",
            max_context_tokens=32000,
            max_tokens=1024,
            system_prompt="Be brief.",
            rationale="OMN-19996 repair",
        )
    )
    first = next(e for e in events if isinstance(e, ModelInferenceIntent))
    repairs = handler.handle_inference_response(
        ModelInferenceResponseData(
            correlation_id=cid,
            inference_attempt_id=first.inference_attempt_id,
            content="not valid JSON",
            model_used="m-1",
            latency_ms=10,
            credential_source=first_source,
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
        )
    )
    repair = next(e for e in repairs if isinstance(e, ModelInferenceIntent))
    assert repair.inference_attempt_id != first.inference_attempt_id
    handler.handle_inference_response(
        ModelInferenceResponseData(
            correlation_id=cid,
            inference_attempt_id=repair.inference_attempt_id,
            content="",
            model_used="m-1",
            latency_ms=1,
            error_message="provider credential unavailable before request",
            credential_source=repair_source,
        )
    )
    history = handler.workflows[cid].escalation_history
    refs = [row.secret_ref for row in history if row.secret_ref]
    assert history, "the rung must have been recorded"
    assert refs == ([expected] if expected else [])


def test_the_binding_and_proofs_survive_the_durable_state_codec() -> None:
    """A proof recorded in one dispatch reaches the row written in a later one.

    The deployed runtime persists workflow state between dispatches through
    the state codec, so the binding and the proofs must round-trip exactly.
    """
    from omnimarket.nodes.node_delegation_orchestrator import state_codec

    handler, cid, attempt_id = _routed(X)
    workflow = handler.workflows[cid]
    workflow.proven_attempt_refs[attempt_id] = X
    restored = state_codec.decode(state_codec.encode(workflow))
    assert restored.current_attempt_intent_ref == (attempt_id, X)
    assert restored.proven_attempt_refs == {attempt_id: X}
