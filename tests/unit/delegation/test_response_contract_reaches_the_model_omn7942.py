# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The declared response contract reaches the MODEL, not only the gate (OMN-7942).

Measured defect, 2026-09-18, correlation ``4c053fe9-1fce-4204-8705-2ed009fdc32d``
on the live ``.201`` lab endpoint: a caller that declares a ``response_contract``
gets its answer graded against a schema the model was never shown. The served
model's own recorded reasoning said so in the first person -- "We have no
explicit schema." -- and then guessed a key name the contract does not contain.
Three local attempts failed the deterministic floor, the router climbed to
``cheap_cloud`` and spent $0.003856, and the run terminated failed.

The control that rules out a model-quality reading: the 2026-09-18 ground-truth
probe saw glm-5.3-flash and gemini-2.5-flash-lite, two different models on two
different providers, fail the identical gate in the same run. A defect that
reproduces across three vendors is a defect in our own path.

``response_contract`` flowed CLI -> ``ModelDelegateSkillRequest`` -> dispatch
port -> quality gate, and stopped there: ``handler_llm_delegation_call`` carried
no reference to it at all, so no part of the outbound chat-completions payload
ever mentioned the schema.

These tests drive the REAL handler + REAL dispatch port + REAL quality-gate
reducer with only the bifrost backends list patched, in the OMN-14208
cross-boundary style of the OMN-15193 seam suite beside them. The injected
effect records the ``ModelLlmDelegationCallRequest`` it is handed, so the
assertion is against the actual outbound system prompt rather than a stand-in.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from omnimarket.delegation.response_contract_instruction import (
    compose_system_prompt_with_response_contract,
    compose_system_prompt_with_response_contract_instruction,
    render_response_contract_instruction,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)
from omnimarket.routing import delegation_backend_resolution

_ENDPOINT = "http://stickybeatz-studio:8401/v1/chat/completions"
_MODEL_ID = "mlx-community/Qwen3.6-35B-A3B-8bit"

# The classifier contract from the OMN-18625 register lane -- the exact shape
# that failed twelve times out of twelve.
_CLASSIFIER_CONTRACT: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["category", "confidence"],
    "properties": {
        "category": {
            "type": "string",
            "enum": ["ruling", "decision-requested", "consent", "none"],
        },
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
    },
}

_CONFORMING_ANSWER = json.dumps({"category": "ruling", "confidence": 0.91})


def _backends() -> list[dict[str, Any]]:
    return [
        {
            "backend_id": "local-coder-mlx",
            "endpoint_url": _ENDPOINT,
            "model_name": _MODEL_ID,
            "tier": "local",
            "max_tokens": 65536,
            "timeout_ms": 300000,
            "capabilities": ["agent_delegation", "reasoning"],
        },
    ]


class _RecordingEffect:
    """Injected effect handler that records the outbound request verbatim."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[ModelLlmDelegationCallRequest] = []

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        self.calls.append(request)
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content=self.content,
            tokens_in=11,
            tokens_out=22,
            latency_ms=5,
            actual_cost_usd=Decimal("0"),
            savings_usd=Decimal("0"),
        )


def _make_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, content: str
) -> tuple[HandlerDelegateSkill, _RecordingEffect]:
    monkeypatch.setattr(
        delegation_backend_resolution,
        "load_bifrost_backends",
        lambda **_: _backends(),
    )
    effect = _RecordingEffect(content)
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )
    return HandlerDelegateSkill(dispatch_port=port), effect


# --------------------------------------------------------------------------
# The renderer itself
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_the_rendered_instruction_names_every_required_key() -> None:
    """The measured failure was the model guessing a key name. The instruction
    names the required keys explicitly rather than leaving them to be read out
    of the schema body, because the guess in the recorded reasoning
    (``{"classification": ...}`` / ``{"label": ...}``) was of a key NAME."""
    rendered = render_response_contract_instruction(_CLASSIFIER_CONTRACT)

    assert "category" in rendered
    assert "confidence" in rendered
    # The schema body itself is present, not only a summary of it.
    assert '"enum"' in rendered
    assert "decision-requested" in rendered


@pytest.mark.unit
def test_the_rendered_instruction_forbids_prose_and_a_code_fence() -> None:
    """Instructing a schema without forbidding the wrapper trades one failure
    mode for another: a model told to emit JSON very commonly fences it."""
    rendered = render_response_contract_instruction(_CLASSIFIER_CONTRACT).lower()

    assert "only" in rendered
    assert "fence" in rendered or "```" in rendered


@pytest.mark.unit
def test_the_rendered_instruction_preserves_required_optional_and_extra_key_rules() -> (
    None
):
    """The renderer must not turn a permissive schema into a closed one."""
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "required_key": {"type": "string"},
            "optional_key": {"type": "number"},
        },
        "required": ["required_key"],
        "additionalProperties": True,
    }

    rendered = render_response_contract_instruction(schema)

    assert "required_key" in rendered
    assert "optional_key" in rendered
    assert "Additional properties are permitted" in rendered
    assert "exactly these keys" not in rendered


@pytest.mark.unit
def test_the_rendered_instruction_requires_no_undeclared_keys_for_closed_schema() -> (
    None
):
    """A closed schema must be rendered as closed for the model as well."""
    rendered = render_response_contract_instruction(_CLASSIFIER_CONTRACT)

    assert "Do not include keys other than the declared properties" in rendered


@pytest.mark.unit
@pytest.mark.parametrize(
    ("output_shape", "expected"),
    [
        ("markdown", "Markdown deliverable"),
        ("plain_text", "plain-text deliverable"),
    ],
)
def test_the_rendered_instruction_honors_declared_text_output_shape(
    output_shape: str, expected: str
) -> None:
    rendered = render_response_contract_instruction(
        {"x-omninode-output-shape": output_shape},
        render_start_marker="FINAL:",
    )

    assert expected in rendered
    assert "JSON Schema" not in rendered
    assert "FINAL:" in rendered


@pytest.mark.unit
def test_the_rendering_is_deterministic_under_key_insertion_order() -> None:
    """Two dicts that differ only in KEY INSERTION order render byte-identically,
    so the instruction cannot vary run to run for the same declared contract.

    The claim is deliberately bounded to mapping key order. A JSON array is an
    ordered document node, so a contract whose ``required`` list is written in
    a different order is a different document and is rendered as written --
    reordering it here would show the model a schema its caller did not
    declare.
    """
    reordered: dict[str, Any] = {
        "properties": _CLASSIFIER_CONTRACT["properties"],
        "required": _CLASSIFIER_CONTRACT["required"],
        "type": "object",
        "additionalProperties": False,
    }
    assert list(reordered) != list(_CLASSIFIER_CONTRACT)

    assert render_response_contract_instruction(
        _CLASSIFIER_CONTRACT
    ) == render_response_contract_instruction(reordered)


@pytest.mark.unit
def test_composing_with_no_contract_returns_the_base_prompt_unchanged() -> None:
    """The no-contract path is byte-preserving: every caller that declares no
    contract sends exactly the system prompt it sent before this change."""
    base = "You are a careful research assistant."

    assert (
        compose_system_prompt_with_response_contract(
            system_prompt=base, response_contract=None
        )
        == base
    )


@pytest.mark.unit
def test_composing_an_resolved_instruction_preserves_its_exact_bytes() -> None:
    instruction = render_response_contract_instruction(_CLASSIFIER_CONTRACT)

    composed = compose_system_prompt_with_response_contract_instruction(
        system_prompt="Use the supplied response contract.",
        instruction=instruction,
    )

    assert composed.endswith(instruction)


# --------------------------------------------------------------------------
# The seam: does it reach the model?
# --------------------------------------------------------------------------


@pytest.mark.unit
async def test_a_caller_declared_contract_reaches_the_outbound_system_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE defect. Before this change the outbound system prompt never
    mentioned the schema, which is why the served model said it had none."""
    handler, effect = _make_handler(tmp_path, monkeypatch, content=_CONFORMING_ANSWER)
    request = ModelDelegateSkillRequest(
        prompt="Classify this ledger row.",
        task_type="reasoning",
        source="claude-code",
        backend_id="local-coder-mlx",
        response_contract=_CLASSIFIER_CONTRACT,
    )

    response = await handler.handle(request)

    assert response.status == "completed"
    assert len(effect.calls) == 1
    system_prompt = effect.calls[0].system_prompt or ""
    assert "category" in system_prompt
    assert "confidence" in system_prompt
    assert "decision-requested" in system_prompt


@pytest.mark.unit
async def test_no_declared_contract_leaves_the_system_prompt_free_of_schema_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A default text contract conveys its marker without inventing a schema."""
    handler, effect = _make_handler(
        tmp_path, monkeypatch, content="A plain prose answer about the row."
    )
    request = ModelDelegateSkillRequest(
        prompt="Summarize this ledger row.",
        task_type="summarization",
        source="claude-code",
        backend_id="local-coder-mlx",
    )

    await handler.handle(request)

    assert len(effect.calls) >= 1
    system_prompt = effect.calls[0].system_prompt or ""
    assert "JSON Schema" not in system_prompt
    assert "### ANSWER" in system_prompt


@pytest.mark.unit
async def test_the_schema_shown_is_the_schema_graded_against(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The anti-drift property, and the reason the effective contract is
    resolved ONCE and threaded to both surfaces.

    Before this change the task-class default was resolved inside
    ``_evaluate_quality_gate`` -- that is, AFTER the model had already answered
    -- so a class-defaulted contract could not have been shown to the model
    even in principle. Resolving it once, before the call, is what makes
    "instructed" and "graded" the same value by construction rather than by two
    call sites agreeing.

    ``agent_delegation`` declares a task-class default contract
    (``response_contract_ref``, OMN-15161/OMN-15196), so this drives the
    class-default branch with no caller-declared contract at all.
    """
    handler, effect = _make_handler(tmp_path, monkeypatch, content=_CONFORMING_ANSWER)
    request = ModelDelegateSkillRequest(
        prompt="Report on the dispatched work.",
        task_type="agent_delegation",
        source="claude-code",
        backend_id="local-coder-mlx",
    )

    response = await handler.handle(request)

    # The classifier-shaped answer has no schema-conforming JSON value for the
    # dispatch-report class. The response boundary removes it before gate
    # evaluation and records the typed refusal; the model still saw the exact
    # schema that supplied that boundary.
    assert response.status == "failed"
    assert response.response == ""
    assert response.output_refusal is not None
    assert response.output_refusal.reason == "no_schema_conforming_json"
    assert any(
        "SCHEMA_VIOLATION" in failure
        for failure in response.output_refusal.contract_failure_reasons
    )
    system_prompt = effect.calls[0].system_prompt or ""
    assert "JSON Schema" in system_prompt
    assert "DispatchReport" in system_prompt


# --------------------------------------------------------------------------
# What the gate must accept once the model is actually instructed
# --------------------------------------------------------------------------


@pytest.mark.unit
async def test_a_fenced_json_answer_satisfies_the_declared_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Coupled to the change above rather than independent of it.

    Instructing a model to emit only JSON makes a markdown-fenced JSON object
    the single most likely near-miss. The contract evaluator parsed from
    character zero, so a fence failed as ``MALFORMED: response is not valid
    JSON``. Reuses the module's existing fence helper; it is not a new
    heuristic and it is confined to the response-contract branch.
    """
    handler, _ = _make_handler(
        tmp_path,
        monkeypatch,
        content=f"```json\n{_CONFORMING_ANSWER}\n```",
    )
    request = ModelDelegateSkillRequest(
        prompt="Classify this ledger row.",
        task_type="reasoning",
        source="claude-code",
        backend_id="local-coder-mlx",
        response_contract=_CLASSIFIER_CONTRACT,
    )

    response = await handler.handle(request)

    assert response.status == "completed"
    assert response.quality_gate_passed is True


@pytest.mark.unit
async def test_a_reasoning_trace_before_the_answer_is_stripped_before_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PIN on behaviour that already holds, not a change (OMN-18278 crit. 1).

    ``delta`` segments the reasoning preamble at line 1916 before branching
    into the response-contract evaluator, and the evaluator additionally strips
    paired think tags. This test exists because the 2026-09-18 report read the
    12/12 failure as an unstripped preamble; measured, the shape that survives
    is an UNTAGGED prose preamble, which no boundary rule claims and which the
    conveyed instruction -- not a widened strip heuristic -- is the remedy for.
    """
    handler, _ = _make_handler(
        tmp_path,
        monkeypatch,
        content=f"<think>weighing the options</think>\n{_CONFORMING_ANSWER}",
    )
    request = ModelDelegateSkillRequest(
        prompt="Classify this ledger row.",
        task_type="reasoning",
        source="claude-code",
        backend_id="local-coder-mlx",
        response_contract=_CLASSIFIER_CONTRACT,
    )

    response = await handler.handle(request)

    assert response.status == "completed"
    assert response.quality_gate_passed is True


# --------------------------------------------------------------------------
# The untagged prose preamble the served model actually emits
# --------------------------------------------------------------------------

# Verbatim from the live .201 endpoint, Qwen3.8-27B, 2026-09-18, with the
# schema conveyed. The model reads the contract correctly -- it names both
# required keys and picks a valid enum member -- and asserts its own
# compliance ("Ensure JSON only. No markdown.") inside the very preamble that
# breaks it. This is the OMN-18278 self-certifying pattern, which is why the
# remedy is not a better prompt.
_SERVED_MODEL_ANSWER_BEHIND_AN_UNTAGGED_PREAMBLE = (
    "We need answer user's request: classify ledger row into one of "
    "categories: ruling, decision-requested, consent, none. Need output only "
    "JSON object with category and confidence.\n\n"
    'We need classify. The text says "the operator ruled that delegation is '
    'used..." This is a ruling? Category likely "ruling". Need confidence '
    "maybe 0.9. Ensure JSON only. No markdown. Need final only JSON.\n\n\n"
    '{\n  "category": "ruling",\n  "confidence": 0.95\n}'
)


@pytest.mark.unit
async def test_the_served_models_untagged_preamble_no_longer_fails_a_conforming_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The residual the conveyed schema exposed rather than removed.

    Before this change the evaluator parsed from character zero, so this
    response -- which CONTAINS a fully conforming object -- failed as
    MALFORMED. Three local attempts failed that way on every run and the
    router climbed to a metered cloud rung each time.
    """
    handler, _ = _make_handler(
        tmp_path,
        monkeypatch,
        content=_SERVED_MODEL_ANSWER_BEHIND_AN_UNTAGGED_PREAMBLE,
    )
    request = ModelDelegateSkillRequest(
        prompt="Classify this ledger row.",
        task_type="reasoning",
        source="claude-code",
        backend_id="local-coder-mlx",
        response_contract=_CLASSIFIER_CONTRACT,
    )

    response = await handler.handle(request)

    assert response.status == "completed"
    assert response.quality_gate_passed is True


@pytest.mark.unit
def test_extraction_is_bounded_by_the_schema_and_not_by_prose_shape() -> None:
    """The positive control on the search, and its refusal.

    A candidate is taken only if it PARSES and VALIDATES, so the search cannot
    pick up an arbitrary brace-delimited fragment. A response whose only
    embedded JSON violates the schema is not rescued into a pass.
    """
    from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
        _schema_conforming_json_in,
    )

    # Positive control: a conforming object behind prose IS found.
    located = _schema_conforming_json_in(
        f"Thinking about it. {_CONFORMING_ANSWER}", _CLASSIFIER_CONTRACT
    )
    assert located is not None
    assert located[0] == {"category": "ruling", "confidence": 0.91}
    assert located[1] is True

    # A decoy object that parses but violates the schema is returned for its
    # violation reasons, never as a conforming candidate.
    decoy = _schema_conforming_json_in(
        'Here is my answer: {"label": "bug"}', _CLASSIFIER_CONTRACT
    )
    assert decoy is not None
    assert decoy[0] == {"label": "bug"}

    # Nothing parseable at all yields None, so the caller still refuses.
    assert (
        _schema_conforming_json_in("no json here at all", _CLASSIFIER_CONTRACT) is None
    )


@pytest.mark.unit
def test_the_last_conforming_value_wins_over_an_earlier_draft() -> None:
    """A model that reasons about the shape before committing emits the draft
    first and the answer last, so the LAST conforming value is the answer."""
    from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
        _schema_conforming_json_in,
    )

    content = (
        'Maybe {"category": "none", "confidence": 0.1}? No, reconsidering.\n'
        '{"category": "ruling", "confidence": 0.95}'
    )
    located = _schema_conforming_json_in(content, _CLASSIFIER_CONTRACT)

    assert located is not None
    assert located[0] == {"category": "ruling", "confidence": 0.95}


@pytest.mark.unit
async def test_a_schema_violating_embedded_value_fails_without_returning_raw_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A schema-violating embedded value is a typed refusal, never a response."""
    handler, effect = _make_handler(
        tmp_path,
        monkeypatch,
        content='After some thought: {"label": "bug"}',
    )
    request = ModelDelegateSkillRequest(
        prompt="Classify this ledger row.",
        task_type="reasoning",
        source="claude-code",
        backend_id="local-coder-mlx",
        response_contract=_CLASSIFIER_CONTRACT,
    )

    response = await handler.handle(request)

    assert response.status == "failed"
    assert response.response == ""
    assert response.output_refusal is not None
    assert response.output_refusal.reason == "no_schema_conforming_json"
    assert any(
        "SCHEMA_VIOLATION" in failure
        for failure in response.output_refusal.contract_failure_reasons
    )
    assert render_response_contract_instruction(_CLASSIFIER_CONTRACT) in (
        effect.calls[0].system_prompt or ""
    )


@pytest.mark.unit
async def test_the_caller_receives_the_conforming_object_not_the_prose_around_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Passing the gate is not the same as serving the caller.

    The register classifier of OMN-18625 calls ``json.loads`` on what it is
    handed. A run that scores 1.0 and returns the model's scratchpad with the
    object buried in it breaks that caller just as hard as a failed run, so
    the response seam substitutes the value the gate graded.
    """
    handler, _ = _make_handler(
        tmp_path,
        monkeypatch,
        content=_SERVED_MODEL_ANSWER_BEHIND_AN_UNTAGGED_PREAMBLE,
    )
    request = ModelDelegateSkillRequest(
        prompt="Classify this ledger row.",
        task_type="reasoning",
        source="claude-code",
        backend_id="local-coder-mlx",
        response_contract=_CLASSIFIER_CONTRACT,
    )

    response = await handler.handle(request)

    assert response.status == "completed"
    assert json.loads(response.response) == {"category": "ruling", "confidence": 0.95}


@pytest.mark.unit
async def test_a_failing_response_is_not_returned_for_diagnosis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A contract failure retains typed evidence while withholding raw text."""
    raw = 'After some thought: {"label": "bug"}'
    handler, _ = _make_handler(tmp_path, monkeypatch, content=raw)
    request = ModelDelegateSkillRequest(
        prompt="Classify this ledger row.",
        task_type="reasoning",
        source="claude-code",
        backend_id="local-coder-mlx",
        response_contract=_CLASSIFIER_CONTRACT,
    )

    response = await handler.handle(request)

    assert response.status == "failed"
    assert response.response == ""
    assert response.output_refusal is not None
    assert response.output_refusal.reason == "no_schema_conforming_json"
    assert any(
        "SCHEMA_VIOLATION" in failure
        for failure in response.output_refusal.contract_failure_reasons
    )
