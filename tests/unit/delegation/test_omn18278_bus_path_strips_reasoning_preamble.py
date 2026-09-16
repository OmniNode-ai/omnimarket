# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-18278: the bus path must hand back the answer, not the scratchpad.

WHAT THIS CLOSES

    OMN-18379 declared the reasoning-preamble boundary in
    ``task_class_contracts.v1.yaml`` and put one pure segmenter behind it. Two
    surfaces read a delegation response: the verification seam (the quality
    gate, which segments inside ``delta``) and the RESPONSE seam (whatever text
    the caller is handed). The bus-less local path applies it at both — the
    local dispatch port rewrites ``result.content`` to the answer segment. The
    message-bus path applied it at the first only: ``handle_inference_response``
    forwarded the raw provider text into the gate intent, and
    ``_record_inference_response`` stored the same raw text on
    ``workflow.inference_content``, which is the exact field the terminal's
    ``content`` is built from.

    So on the bus path the gate judged the answer while the CALLER received the
    scratchpad with the answer somewhere inside it. Dogfood lane
    ``delegation-dogfood-0045`` recorded it on 2026-09-16 as break (B), "bus
    path does not strip the trace at all", and OMN-18278's own status table
    carries it as the remaining half of criterion 1.

WHAT THIS DELIBERATELY DOES NOT CLAIM

    It does not claim the bus path was blind to an output-budget truncation.
    It was not, and the premise this lane was dispatched on said otherwise, so
    the positive control is written down here rather than left as a belief:
    ``HandlerInferenceIntent`` — the effect boundary the bus path runs — refuses
    ``finish_reason=length`` outright, through the same shared predicate
    ``OmniNode-ai/omnimarket#2580`` introduced, and the orchestrator classifies
    that refusal as ``CONTEXT_TOO_LARGE`` and escalates. A truncated draft
    therefore never becomes a bus answer. What was hand-typed rather than shared
    was the orchestrator's half of that handshake: the classifier matched a
    literal copy of the marker the effect's message carries, so the two could
    drift apart silently. It is bound to the shared constant here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.delegation.wire import (
    ModelInferenceIntent,
    ModelInferenceResponseData,
    ModelQualityGateIntent,
    ModelRoutingIntent,
)

from omnimarket.delegation.reasoning_preamble import (
    EnumReasoningBoundaryRule,
    segment_reasoning_preamble,
)
from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.inference.provider_finish_reason import (
    TRUNCATED_RESPONSE_ERROR_MESSAGE,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers import (
    handler_delegation_workflow as bus_workflow_module,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    TRUNCATED_RESPONSE_FAILURE_MARKER,
    HandlerDelegationWorkflow,
    _inference_error_failure_class,
    _segmented_inference_response,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_result import (
    ModelDelegationResult,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate_intent import (
    HandlerQualityGateIntent,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_routing_intent import (
    HandlerRoutingIntent,
)

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "configs"
    / "task_class_contracts.v1.yaml"
)

# Same minimal bifrost contract the other real-dispatch-path tests in this
# directory use, so the routing reducer resolves a decision without credentials.
_BIFROST_SUMMARIZATION = (
    "config_version: '2.0.0'\n"
    "schema_version: bifrost_delegation.v1\n"
    "backends:\n"
    "  - backend_id: cloud-gemini-flash\n"
    "    provider: gemini\n"
    '    endpoint_url: "http://test-summarizer:8000/v1/chat/completions"\n'
    '    model_name: "gemini-2.5-flash-lite"\n'
    "    tier: cheap_cloud\n"
    "    timeout_ms: 30000\n"
    "    capabilities: [summarization, simple_tasks, document, code_generation]\n"
    "routing_rules:\n"
    '  - rule_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"\n'
    "    priority: 10\n"
    "    task_class: summarization\n"
    '    task_class_contract_version: "1.0.0"\n'
    '    backend_policy_version: "2.0.0"\n'
    "    match_operation_types: [chat_completion]\n"
    "    match_capabilities: [summarization]\n"
    "    backend_ids: [cloud-gemini-flash]\n"
    "    fallback_policy:\n"
    "      action: escalate_to_next_tier\n"
    "      max_retries: 1\n"
    "      on_exhaust: return_error\n"
    '    shadow_policy_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"\n'
    "default_backends:\n"
    "  - cloud-gemini-flash\n"
    "circuit_breaker:\n"
    "  failure_threshold: 5\n"
    "  window_seconds: 30\n"
    "failover:\n"
    "  max_attempts: 3\n"
    "  backoff_base_ms: 500\n"
    "shadow_mode:\n"
    "  enabled: false\n"
    '  policy_version: "test"\n'
    "  log_sample_rate: 1.0\n"
    "  comparison_logging_enabled: true\n"
    "  max_shadow_latency_ms: 5.0\n"
)

# The shape lane ``delegation-dogfood-0045`` recorded: a plain-text scratchpad
# the model opened with a declared lead-in and closed with a trace terminator
# that has no opener, followed by the real answer. The paired-tag strip cannot
# see it; the OMN-18379 segmenter cuts at the terminator.
_ANSWER = (
    "The quality gate now segments a leaked reasoning scratchpad off the front "
    "of a delegated response before any content rule reads it, so a blocking "
    "phrase rule judges the answer rather than the model's notes to itself."
)
_SCRATCHPAD = (
    "Here's a thinking process: the user is asking for a summary. Let me think "
    "through what matters. I should not show my reasoning, so I will keep this "
    "to myself. Some of this is unverified, so I will hedge internally only.\n"
    "</think>\n"
)
_LEAKED_RESPONSE = f"{_SCRATCHPAD}{_ANSWER}"

# A well-formed response carrying no declared boundary. Nothing may be cut from
# it: "no boundary found" is a real answer, not a licence to guess.
_CLEAN_RESPONSE = (
    "The change binds the bus orchestrator's truncation classifier to the "
    "shared constant the inference effect raises, so the two cannot drift."
)


def _task_class_dod(task_class: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    contract = yaml.safe_load(_CONTRACT_PATH.read_text())
    dod = contract["task_classes"][task_class]["definition_of_done"]
    return (tuple(dod.get("deterministic", ())), tuple(dod.get("heuristic", ())))


def _inference_response(
    intent: ModelInferenceIntent, content: str
) -> ModelInferenceResponseData:
    """The inference effect's OUTPUT event, constructed as a controlled DTO.

    The model/HTTP boundary is not faked: what is under test is the
    orchestrator's handling of a response it has been handed, which is
    deterministic downstream logic.
    """
    return ModelInferenceResponseData(
        correlation_id=intent.correlation_id,
        inference_attempt_id=intent.inference_attempt_id,
        content=content,
        model_used=intent.model,
        llm_call_id="chatcmpl-test",
        latency_ms=1,
        prompt_tokens=64,
        completion_tokens=128,
        total_tokens=192,
    )


@pytest.mark.unit
class TestBusPathSegmentsTheResponse:
    """The bus path's caller-facing text is the answer segment, not the whole."""

    @pytest.fixture(autouse=True)
    def _bifrost_contract(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing as _h

        _h._config = None
        _h._load_bifrost_endpoints.cache_clear()
        contract_path = tmp_path / "bifrost_delegation.yaml"
        contract_path.write_text(_BIFROST_SUMMARIZATION, encoding="utf-8")
        monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract_path))
        yield
        _h._config = None
        _h._load_bifrost_endpoints.cache_clear()

    @pytest.fixture
    def workflow(self) -> HandlerDelegationWorkflow:
        return HandlerDelegationWorkflow(workflows={})

    @pytest.fixture
    def request_dto(self) -> ModelDelegationRequest:
        return ModelDelegationRequest(
            prompt="Summarize what the reasoning-preamble boundary rule changed.",
            task_type="summarization",
            correlation_id=uuid4(),
            max_tokens=512,
            emitted_at=datetime.now(UTC),
        )

    def _drive_to_gate_intent(
        self,
        workflow: HandlerDelegationWorkflow,
        request: ModelDelegationRequest,
        content: str,
    ) -> ModelQualityGateIntent:
        """Drive request -> routing -> inference-response through REAL handlers."""
        routing_intents = workflow.handle_delegation_request(request)
        assert isinstance(routing_intents[0], ModelRoutingIntent)
        decision = HandlerRoutingIntent().handle(routing_intents[0])
        inference_intents = workflow.handle_routing_decision(decision)
        assert isinstance(inference_intents[0], ModelInferenceIntent)
        response = _inference_response(inference_intents[0], content)
        gate_intents = workflow.handle_inference_response(response)
        assert len(gate_intents) == 1
        gate_intent = gate_intents[0]
        assert isinstance(gate_intent, ModelQualityGateIntent)
        return gate_intent

    def test_gate_intent_carries_the_answer_not_the_scratchpad(
        self,
        workflow: HandlerDelegationWorkflow,
        request_dto: ModelDelegationRequest,
    ) -> None:
        """The text the bus sends to the gate is the answer segment."""
        gate_intent = self._drive_to_gate_intent(
            workflow, request_dto, _LEAKED_RESPONSE
        )

        assert gate_intent.payload.llm_response_content == _ANSWER, (
            "the bus gate intent must carry the answer segment; got "
            f"{gate_intent.payload.llm_response_content[:120]!r}"
        )
        assert "thinking process" not in gate_intent.payload.llm_response_content

    def test_workflow_content_is_the_answer_so_the_terminal_is(
        self,
        workflow: HandlerDelegationWorkflow,
        request_dto: ModelDelegationRequest,
    ) -> None:
        """``workflow.inference_content`` is the field the terminal is built from.

        ``_terminal_quality_inputs`` sets ``content=workflow.inference_content or
        ""``, so this field IS the caller-facing artifact on the bus path.
        """
        self._drive_to_gate_intent(workflow, request_dto, _LEAKED_RESPONSE)
        state = workflow._workflows[request_dto.correlation_id]

        assert state.inference_content == _ANSWER, (
            "the recorded content the terminal is built from must be the answer "
            f"segment; got {state.inference_content[:120]!r}"
        )

    def test_terminal_content_is_the_answer(
        self,
        workflow: HandlerDelegationWorkflow,
        request_dto: ModelDelegationRequest,
    ) -> None:
        """End to end: the emitted terminal's content carries no scratchpad."""
        gate_intent = self._drive_to_gate_intent(
            workflow, request_dto, _LEAKED_RESPONSE
        )
        gate_result = HandlerQualityGateIntent().handle(gate_intent)
        terminals = [
            event
            for event in workflow.handle_gate_result(gate_result)
            if isinstance(event, ModelDelegationResult)
        ]

        assert terminals, "the chain must emit a terminal delegation result"
        for terminal in terminals:
            assert "thinking process" not in terminal.content, (
                "a bus terminal must not hand the caller the model's scratchpad; "
                f"content={terminal.content[:160]!r}"
            )

    def test_a_response_with_no_declared_boundary_is_untouched(
        self,
        workflow: HandlerDelegationWorkflow,
        request_dto: ModelDelegationRequest,
    ) -> None:
        """Nothing is cut on a guess — the non-regression direction."""
        gate_intent = self._drive_to_gate_intent(workflow, request_dto, _CLEAN_RESPONSE)
        state = workflow._workflows[request_dto.correlation_id]

        assert gate_intent.payload.llm_response_content == _CLEAN_RESPONSE
        assert state.inference_content == _CLEAN_RESPONSE

    def test_an_error_response_is_not_rewritten(
        self,
        workflow: HandlerDelegationWorkflow,
        request_dto: ModelDelegationRequest,
    ) -> None:
        """An inference FAILURE carries no content and must reach its own branch.

        Segmenting must not swallow the error path: the escalation branch reads
        ``error_message``, and a response that never produced content has
        nothing to segment.
        """
        routing_intents = workflow.handle_delegation_request(request_dto)
        decision = HandlerRoutingIntent().handle(routing_intents[0])
        inference_intents = workflow.handle_routing_decision(decision)
        intent = inference_intents[0]
        assert isinstance(intent, ModelInferenceIntent)
        failure = ModelInferenceResponseData(
            correlation_id=intent.correlation_id,
            inference_attempt_id=intent.inference_attempt_id,
            content="",
            model_used=intent.model,
            error_message=TRUNCATED_RESPONSE_ERROR_MESSAGE,
        )

        emitted = workflow.handle_inference_response(failure)

        assert not any(
            isinstance(event, ModelQualityGateIntent) for event in emitted
        ), "a failed inference must not be forwarded to the quality gate"


@pytest.mark.unit
class TestBusPathReusesTheSharedImplementations:
    """One segmenter and one truncation constant, not a second copy of each."""

    def test_the_orchestrator_uses_the_shared_segmenter_by_identity(self) -> None:
        """A second regex in the orchestrator would make this red."""
        assert (
            bus_workflow_module.segment_reasoning_preamble is segment_reasoning_preamble
        ), (
            "the bus orchestrator must segment with the shared OMN-18379 "
            "function, not a local copy of the boundary rule"
        )

    def test_the_seam_helper_returns_the_same_segmentation(self) -> None:
        """The helper's rewrite equals the shared segmenter's own answer."""
        response = ModelInferenceResponseData(
            correlation_id=uuid4(),
            content=_LEAKED_RESPONSE,
            model_used="test-model",
        )
        segmentation = segment_reasoning_preamble(_LEAKED_RESPONSE)
        assert (
            segmentation.boundary_rule is EnumReasoningBoundaryRule.UNPAIRED_CLOSING_TAG
        )

        assert _segmented_inference_response(response).content == segmentation.answer

    def test_the_seam_helper_is_idempotent(self) -> None:
        """Applying it twice cannot compound — the property OMN-18379 declares."""
        response = ModelInferenceResponseData(
            correlation_id=uuid4(),
            content=_LEAKED_RESPONSE,
            model_used="test-model",
        )
        once = _segmented_inference_response(response)

        assert _segmented_inference_response(once).content == once.content

    def test_the_truncation_marker_is_derived_from_the_shared_constant(self) -> None:
        """The classifier matches the effect's own message, not a copy of it."""
        assert TRUNCATED_RESPONSE_FAILURE_MARKER in (
            TRUNCATED_RESPONSE_ERROR_MESSAGE.lower()
        ), (
            "the bus classifier's marker must come from the shared constant the "
            "inference effect raises"
        )

    def test_the_effects_truncation_message_still_classifies_as_context(self) -> None:
        """Positive control: the bus DOES act on a truncation, at the effect seam.

        ``HandlerInferenceIntent`` refuses ``finish_reason=length`` and the
        orchestrator escalates it. This pins the handshake end to end so that
        rewording either half is a red test rather than a silent reclassification
        to ``UNKNOWN``.
        """
        assert (
            _inference_error_failure_class(TRUNCATED_RESPONSE_ERROR_MESSAGE)
            is EnumDelegationFailureClass.CONTEXT_TOO_LARGE
        )
