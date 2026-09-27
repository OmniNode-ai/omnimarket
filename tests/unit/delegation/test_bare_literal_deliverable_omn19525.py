# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-19525: a correct bare literal answer is the deliverable, not a blank.

The documented liveness control is the prompt "Reply with exactly the word
READY". A model that obeys replies ``READY`` and nothing else: no marker line,
because there is nothing to mark. ``extract_deliverable`` refused that as
``ambiguous_unmarked_deliverable``, the dispatch port set the content to the
empty string, and the gate graded the empty string as MALFORMED. On 2026-09-25
the control read a live dev lane as dead twice (runs 9071c52f and 9e690e84),
and two capability-matrix trials lost short honest answers the same way (runs
389cbbe1 and ba3120dc).

WHAT CHANGES. Only a request that DECLARED a single-word or exact-literal
answer (``EnumRequestedResponseShape``, resolved from the prompt by the
contract's ``response_shape_directives``) may have its bare, single-line reply
accepted without a marker. Everything else is refused exactly as before, and a
multi-line reply (reasoning, then the literal) is still never guessed at.
"""

from __future__ import annotations

from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import pytest

from omnimarket.delegation.deliverable_extraction import (
    EnumDeliverableExtractionRefusal,
    ModelDeliverableContract,
    canonical_deliverable_contract_sha256,
    extract_deliverable,
    resolve_task_class_deliverable_contract,
)
from omnimarket.enums.enum_requested_response_shape import EnumRequestedResponseShape
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    DelegationWorkflowState,
    _extract_effective_deliverable,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_response_data import (
    ModelInferenceResponseData,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    resolve_requested_shape_for_prompt,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)

pytestmark = pytest.mark.unit

_EXACT = EnumRequestedResponseShape.EXACT_LITERAL
_WORD = EnumRequestedResponseShape.SINGLE_WORD
_FREE = EnumRequestedResponseShape.UNCONSTRAINED


@pytest.fixture(name="contract")
def _contract() -> ModelDeliverableContract:
    """The document class's default text contract: marker-bounded."""
    return resolve_task_class_deliverable_contract("document", None)


def test_the_liveness_prompt_resolves_exact_literal() -> None:
    """The READY control is a declared exact literal, so the new rule applies."""
    shape = resolve_requested_shape_for_prompt("Reply with exactly the word READY")
    assert shape is _EXACT


# ---------------------------------------------------------------------------
# AC1: the bare literal is returned, not blanked.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["READY", "READY\n", "  READY  \n", "\nREADY"])
def test_a_bare_exact_literal_is_the_deliverable(
    contract: ModelDeliverableContract, raw: str
) -> None:
    extracted = extract_deliverable(raw, contract, requested_shape=_EXACT)
    assert extracted.refusal is None
    assert extracted.deliverable == "READY"
    assert raw[extracted.deliverable_start : extracted.deliverable_end] == "READY"
    assert extracted.preamble_chars == extracted.deliverable_start
    assert extracted.raw_chars == len(raw)


def test_a_multiword_exact_literal_on_one_line_is_the_deliverable(
    contract: ModelDeliverableContract,
) -> None:
    extracted = extract_deliverable("all good", contract, requested_shape=_EXACT)
    assert extracted.refusal is None
    assert extracted.deliverable == "all good"


def test_a_bare_single_word_is_the_deliverable(
    contract: ModelDeliverableContract,
) -> None:
    extracted = extract_deliverable("CANNOT_EXECUTE", contract, requested_shape=_WORD)
    assert extracted.refusal is None
    assert extracted.deliverable == "CANNOT_EXECUTE"


# ---------------------------------------------------------------------------
# Nothing else changes: no declared shape, or a reply that is not bare.
# ---------------------------------------------------------------------------


def test_without_a_declared_shape_a_bare_word_is_still_refused(
    contract: ModelDeliverableContract,
) -> None:
    extracted = extract_deliverable("READY", contract)
    assert extracted.refusal is EnumDeliverableExtractionRefusal.AMBIGUOUS_UNMARKED
    assert extracted.deliverable == ""
    explicit = extract_deliverable("READY", contract, requested_shape=_FREE)
    assert explicit.refusal is EnumDeliverableExtractionRefusal.AMBIGUOUS_UNMARKED


@pytest.mark.parametrize("shape", [_EXACT, _WORD])
def test_reasoning_then_the_literal_is_still_refused(
    contract: ModelDeliverableContract, shape: EnumRequestedResponseShape
) -> None:
    """Never guessed: the OMN-18278 and OMN-19267 preamble cases stay refused here."""
    raw = "The user wants one word. I should reply READY.\n\nREADY"
    extracted = extract_deliverable(raw, contract, requested_shape=shape)
    assert extracted.refusal is EnumDeliverableExtractionRefusal.AMBIGUOUS_UNMARKED
    assert extracted.deliverable == ""


def test_two_words_are_not_a_single_word(contract: ModelDeliverableContract) -> None:
    extracted = extract_deliverable("READY now", contract, requested_shape=_WORD)
    assert extracted.refusal is EnumDeliverableExtractionRefusal.AMBIGUOUS_UNMARKED


@pytest.mark.parametrize("raw", ["", "   \n\t"])
def test_an_empty_reply_is_still_refused(
    contract: ModelDeliverableContract, raw: str
) -> None:
    extracted = extract_deliverable(raw, contract, requested_shape=_EXACT)
    assert extracted.refusal is EnumDeliverableExtractionRefusal.AMBIGUOUS_UNMARKED
    assert extracted.deliverable == ""


def test_a_marked_reply_still_uses_its_marker(
    contract: ModelDeliverableContract,
) -> None:
    """A marker, when present, still decides the boundary for a declared shape."""
    marker = contract.render_start_marker
    assert marker is not None
    raw = f"thinking about it\n{marker}\nREADY"
    extracted = extract_deliverable(raw, contract, requested_shape=_EXACT)
    assert extracted.deliverable == "READY"


# ---------------------------------------------------------------------------
# The bus workflow reads the declared shape off its routing decision.
# ---------------------------------------------------------------------------


def _workflow_with_shape(
    shape: EnumRequestedResponseShape,
) -> DelegationWorkflowState:
    cid = uuid4()
    contract = resolve_task_class_deliverable_contract("document", None)
    workflow = DelegationWorkflowState(correlation_id=cid)
    workflow.effective_deliverable_contract = contract
    workflow.response_contract_sha256 = canonical_deliverable_contract_sha256(contract)
    workflow.routing_decision = ModelRoutingDecision(
        correlation_id=cid,
        task_type="document",
        selected_model="model-local",
        selected_backend_id=uuid5(NAMESPACE_DNS, "omninode.ai/backends/local"),
        endpoint_url="https://api.example/v1/chat/completions",
        cost_tier="low",
        tier_name="local",
        max_context_tokens=65536,
        max_tokens=65536,
        system_prompt="You are a writing assistant.",
        rationale="Routed via tier 'local'.",
        requested_shape=shape,
    )
    return workflow


def _response(cid: UUID, content: str) -> ModelInferenceResponseData:
    return ModelInferenceResponseData(
        correlation_id=cid,
        content=content,
        model_used="model-local",
        llm_call_id="chatcmpl-omn19525",
        latency_ms=10,
        prompt_tokens=5,
        completion_tokens=1,
        total_tokens=6,
    )


def test_the_workflow_keeps_a_bare_literal_for_a_declared_shape() -> None:
    workflow = _workflow_with_shape(_EXACT)
    located, refusal, _ = _extract_effective_deliverable(
        workflow, _response(workflow.correlation_id, "READY")
    )
    assert refusal is None
    assert located.content == "READY"


def test_the_workflow_still_blanks_an_unmarked_reply_without_a_shape() -> None:
    workflow = _workflow_with_shape(_FREE)
    located, refusal, _ = _extract_effective_deliverable(
        workflow, _response(workflow.correlation_id, "READY")
    )
    assert refusal is not None
    assert located.content == ""
