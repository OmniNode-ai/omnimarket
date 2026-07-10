# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Golden round-trip test for the durable ``DelegationWorkflowState`` codec.

OMN-14208: ``DelegationWorkflowState`` is a stdlib ``@dataclass`` (nested
Pydantic models, UUID, enum, lists) with no ``model_dump()`` /
``model_validate_json()`` — ``pydantic.TypeAdapter`` is the only correct
codec. This test builds a fully-populated RECEIVED -> ... -> COMPLETED state
with EVERY field non-default (including nested ``ModelRoutingDecision`` /
``ModelQualityGateResult``, UUIDs, enums, and escalation history), round-trips
it through ``state_codec.encode`` / ``decode``, and asserts field-by-field
equality. A silently-dropped field here would re-introduce the tenant-default
leak the durable state design closes (OMN-14058 -> OMN-14208): a cold-process
reload that loses ``tenant_id`` falls back to the shared 'omninode' column
default on every downstream projection.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from uuid import NAMESPACE_DNS, uuid4, uuid5

from omnibase_core.enums.enum_invocation_kind import EnumInvocationKind
from omnibase_core.models.delegation.model_invocation_command import (
    ModelInvocationCommand,
)

from omnimarket.models.delegation.wire.model_quality_gate import ModelQualityGateResult
from omnimarket.nodes.node_delegation_orchestrator import state_codec
from omnimarket.nodes.node_delegation_orchestrator.enums import EnumDelegationState
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    DelegationWorkflowState,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_escalation_attempt import (
    ModelDelegationEscalationAttempt,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)


def _build_fully_populated_state() -> DelegationWorkflowState:
    """Build a COMPLETED workflow state with every field set to a non-default
    value, including nested Pydantic models, UUIDs, enums, and history.
    """
    correlation_id = uuid4()

    request = ModelDelegationRequest(
        prompt="Write unit tests for the round-trip codec.",
        task_type="code_generation",
        source_session_id="session-abc123",
        source_file_path="src/omnimarket/nodes/node_delegation_orchestrator/state_codec.py",
        context_pack="prior context for the ticket",
        context_pack_hash="deadbeef",
        correlation_id=correlation_id,
        max_tokens=4096,
        emitted_at=datetime.now(UTC),
        output_schema_key=None,
        compliance_budget=None,
        quality_contract_mode="extend_task_class",
        acceptance_criteria=("response_non_empty",),
        tenant_id="acme-corp",
    )

    routing_decision = ModelRoutingDecision(
        correlation_id=correlation_id,
        task_type="code_generation",
        selected_model="qwen3-coder-30b",
        selected_backend_id=uuid5(
            NAMESPACE_DNS, "omninode.ai/backends/qwen3-coder-30b"
        ),
        endpoint_url="http://192.168.86.201:8000",  # onex-allow-internal-ip OMN-10865 reason="round-trip test fixture, no live connection"
        api_key_ref="secret-ref",
        extra_headers={"HTTP-Referer": "https://omninode.ai"},
        cost_tier="low",
        max_context_tokens=65536,
        timeout_ms=45000,
        max_tokens=65536,
        system_prompt="You are a test generation assistant.",
        rationale="Task 'code_generation' routed to qwen3-coder-30b.",
        dod_deterministic=("compiles_without_errors",),
        dod_heuristic=("concise",),
        tier_name="local",
    )

    invocation_command = ModelInvocationCommand(
        task_id=uuid4(),
        correlation_id=correlation_id,
        invocation_kind=EnumInvocationKind.MODEL,
        agent_protocol=None,
        model_backend="qwen3-coder-30b",
        target_ref="http://192.168.86.201:8000",  # onex-allow-internal-ip OMN-10865 reason="round-trip test fixture, no live connection"
        payload={},
    )

    gate_result = ModelQualityGateResult(
        correlation_id=correlation_id,
        passed=True,
        fail_category="pass",
        quality_score=0.91,
        failure_reasons=(),
        fallback_recommended=False,
        score_source="combined",
        acceptance_version="v1",
        corpus_hash="corpus-hash-1",
        validator_or_artifact_hash="artifact-hash-1",
        acceptance_command="pytest tests/unit/delegation",
        actual_score=0.91,
        pass_=True,
        failure_cases=(),
        skipped_checks=("no_executor_wired_check",),
    )

    escalation_attempt = ModelDelegationEscalationAttempt(
        tier_name="cheap_cloud",
        model_used="gemini-2.5-flash",
        quality_score=0.62,
        required_bar=0.85,
        actual_score=0.62,
        authority_source="task_class",
        score_source="combined",
        failure_reasons=("score_below_required_bar",),
        prompt_tokens=120,
        completion_tokens=340,
        cost_usd=0.0042,
        latency_ms=1850,
        fallback_recommended=True,
        attempted_at=datetime.now(UTC),
        routing_decision_id=uuid4(),
    )

    return DelegationWorkflowState(
        correlation_id=correlation_id,
        state=EnumDelegationState.COMPLETED,
        request=request,
        routing_decision=routing_decision,
        invocation_command=invocation_command,
        inference_content="def test_foo():\n    assert True",
        inference_model_used="qwen3-coder-30b",
        inference_latency_ms=2137,
        inference_prompt_tokens=210,
        inference_completion_tokens=480,
        inference_total_tokens=690,
        inference_llm_call_id="llm-call-xyz",
        context_pack_hash="deadbeef",
        gate_result=gate_result,
        started_at_ns=1_735_000_000_000_000_000,
        compliance_attempts=2,
        accumulated_tokens=990,
        inference_intent_in_flight=True,
        routing_intent_replayed=True,
        escalation_count=1,
        current_tier_name="local",
        escalation_history=[escalation_attempt],
        cumulative_attempt_cost_usd=0.0042,
        cumulative_attempt_prompt_tokens=120,
        cumulative_attempt_completion_tokens=340,
        tenant_id="acme-corp",
    )


def test_delegation_state_roundtrip_preserves_every_field() -> None:
    """Encode -> decode must reproduce every field of a fully-populated state."""
    original = _build_fully_populated_state()

    encoded = state_codec.encode(original)
    assert isinstance(encoded, bytes)

    decoded = state_codec.decode(encoded)

    # Field-by-field equality: a silently-dropped field would leave `decoded`
    # holding its dataclass default instead of the non-default value built
    # above, and this loop pins each field's identity in the failure message.
    for f in dataclasses.fields(original):
        original_value = getattr(original, f.name)
        decoded_value = getattr(decoded, f.name)
        assert decoded_value == original_value, (
            f"field {f.name!r} did not round-trip: "
            f"original={original_value!r} decoded={decoded_value!r}"
        )

    # Whole-object equality as a belt-and-suspenders check (the dataclass's
    # generated __eq__ compares every field at once).
    assert decoded == original


def test_delegation_state_roundtrip_encodes_well_known_key_interface() -> None:
    """OMN-14208 pair-verify M2: the omnibase_infra state_io wiring seam never
    decodes this payload's business shape — it extracts exactly 3 well-known
    TOP-LEVEL JSON keys (``tenant_id`` / ``state`` / ``in_flight``,
    handler_wiring.py ``_extract_state_io_metadata``) to populate its
    denormalized columns. The dataclass's own field-by-field loop above can't
    catch a well-known-KEY gap: ``DelegationWorkflowState`` has no
    ``in_flight`` field at all (the real field is
    ``inference_intent_in_flight``), so a bare TypeAdapter dump left that
    column permanently False in every persisted row. Assert the wire shape
    directly instead of trusting the dataclass round-trip alone.
    """
    original = _build_fully_populated_state()
    assert original.inference_intent_in_flight is True

    encoded = state_codec.encode(original)
    parsed = json.loads(encoded)

    assert parsed["tenant_id"] == original.tenant_id
    assert parsed["state"] == original.state.value
    assert parsed["in_flight"] is True

    # decode() must strip the well-known key before validating -- it has no
    # corresponding dataclass field -- and still round-trip the real one.
    decoded = state_codec.decode(encoded)
    assert decoded.inference_intent_in_flight is True


def test_delegation_state_roundtrip_decode_accepts_str() -> None:
    """``decode`` must also accept the ``str`` form TypeAdapter.validate_json allows."""
    original = _build_fully_populated_state()
    encoded_bytes = state_codec.encode(original)
    decoded_from_str = state_codec.decode(encoded_bytes.decode("utf-8"))
    assert decoded_from_str == original
