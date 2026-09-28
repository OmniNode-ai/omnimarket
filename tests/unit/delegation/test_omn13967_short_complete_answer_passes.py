# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-13967: a short answer passes only when the request declared a short answer.

``semantic_adequacy`` refuses a bare single token with no terminal punctuation.
Two recorded dev-lane runs show what that rule costs when the request asked for
exactly that:

* run ``1aeccaa6`` (``tests/fixtures/receipts/omn13967_say_ok_1aeccaa6.json``):
  the prompt was ``say ok``, the local model answered ``ok`` in 310 ms, and the
  gate refused it as a single-word fragment;
* correlation ``f037b9be`` (``tests/fixtures/receipts/omn19016_ready_probe_f037b9be.json``):
  the prompt asked for exactly one word, the model answered ``READY``, and four
  rungs refused it the same way.

The acceptance authority for those answers is the request, never the response.
A prompt that declares its own answer shape, through the closed
``response_shape_directives`` set in ``task_class_contracts.v1.yaml``, has its
heuristic band replaced by the declared shape's band, whose adequacy authority
is ``short_form_adequacy``. That check keeps every rule that detects an answer
cut short and drops only the single-word rule the prompt overrode.

``finish_reason=stop`` is not that authority, and this module pins that it is
not. omnimarket#2866 briefly let it lift the single-word rule. The signal only
says generation ended normally: it also fires on a configured stop sequence, and
the gate never sees the prompt, so it cannot tell ``say ok`` from "write the
README". Under that rule ``Done``, ``Yes`` and ``Sure`` passed a ``document``
class prose request at score 1.0 (plan review finding F2, 2026-09-25).

The OMN-18278 set is re-checked here too: a ``length`` finish is still vetoed
whatever the text looks like, including a one-word text.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from omnimarket.enums.enum_provider_finish_reason import EnumProviderFinishReason
from omnimarket.enums.enum_requested_response_shape import EnumRequestedResponseShape
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models import (
    ModelQualityGateInput,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    resolve_requested_shape_for_prompt,
    resolve_task_class_dod_checks,
)

pytestmark = pytest.mark.unit

_RECEIPTS = Path(__file__).resolve().parents[2] / "fixtures" / "receipts"
_SAY_OK = _RECEIPTS / "omn13967_say_ok_1aeccaa6.json"
_READY = _RECEIPTS / "omn19016_ready_probe_f037b9be.json"

_CORRELATION_ID = UUID("9618db94-9077-4a3c-b734-94bd8f372fe3")

# The verdict prefix the single-word rule carries, spelled here rather than
# imported so this module collects against any version of the handler.
_SHAPE_REFUSED = "SHAPE_REFUSED"
_SINGLE_WORD_RULE_TEXT = "bare single-word fragment"

# A prose request of the ``document`` class that declares no answer shape. A
# one-word reply to it is not an answer, whatever the provider reports.
_PROSE_PROMPT = (
    "Write the README section that explains how the delegation quality gate "
    "decides whether a short answer is complete, with one worked example."
)

# Every finish reason that is not the ``length`` truncation veto. None of them
# may change a content verdict: the verdict comes from the declared shape.
_NON_LENGTH_FINISH_REASONS = [
    reason
    for reason in EnumProviderFinishReason
    if reason is not EnumProviderFinishReason.LENGTH
]


def _load(path: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(path.read_text())
    return loaded


def _recorded_gate_input(path: Path) -> ModelQualityGateInput:
    """The gate input exactly as the recorded run built it (class DoD bands)."""
    request = _load(path)["request"]
    return ModelQualityGateInput(
        correlation_id=_CORRELATION_ID,
        task_type=request["task_type"],
        llm_response_content=request["llm_response_content"],
        dod_deterministic=tuple(request["dod_deterministic"]),
        dod_heuristic=tuple(request["dod_heuristic"]),
    )


def _contract_gate_input(
    task_type: str, prompt: str, content: str
) -> ModelQualityGateInput:
    """The gate input the routing authority builds today for ``prompt``.

    The DoD bands come from ``resolve_task_class_dod_checks``, the same
    resolution the bus routing reducer and the bus-less local path feed the gate,
    so a declared shape reaches the gate here exactly as it does in the runtime.
    """
    dod_deterministic, dod_heuristic = resolve_task_class_dod_checks(
        task_type, prompt=prompt
    )
    return ModelQualityGateInput(
        correlation_id=_CORRELATION_ID,
        task_type=task_type,
        llm_response_content=content,
        dod_deterministic=dod_deterministic,
        dod_heuristic=dod_heuristic,
    )


def _replayed_gate_input(path: Path) -> ModelQualityGateInput:
    request = _load(path)["request"]
    return _contract_gate_input(
        request["task_type"], request["prompt"], request["llm_response_content"]
    )


def _prose_gate_input(content: str) -> ModelQualityGateInput:
    return _contract_gate_input("document", _PROSE_PROMPT, content)


def _refused_by_single_word_rule(reasons: tuple[str, ...]) -> bool:
    return any(_SINGLE_WORD_RULE_TEXT in reason for reason in reasons)


# ---------------------------------------------------------------------------
# The recorded fixtures: the premise
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", [_SAY_OK, _READY], ids=["say_ok", "READY"])
def test_the_recorded_run_was_refused_by_the_single_word_rule(fixture: Path) -> None:
    """The premise, read off the recorded terminal rather than restated."""
    recorded = _load(fixture)
    failed = " ".join(attempt["error_message"] for attempt in recorded["attempts"])
    assert _SINGLE_WORD_RULE_TEXT in failed
    assert recorded["terminal"]["quality_gate_passed"] is False


# ---------------------------------------------------------------------------
# AC1: the declared shape is the acceptance authority
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fixture", "shape"),
    [
        (_SAY_OK, EnumRequestedResponseShape.EXACT_LITERAL),
        (_READY, EnumRequestedResponseShape.SINGLE_WORD),
    ],
    ids=["say_ok", "READY"],
)
def test_the_recorded_prompt_declares_a_short_answer(
    fixture: Path, shape: EnumRequestedResponseShape
) -> None:
    """A closed directive in the contract recognises each recorded prompt."""
    prompt = _load(fixture)["request"]["prompt"]
    assert resolve_requested_shape_for_prompt(prompt) is shape


@pytest.mark.parametrize("fixture", [_SAY_OK, _READY], ids=["say_ok", "READY"])
@pytest.mark.parametrize(
    "finish_reason", _NON_LENGTH_FINISH_REASONS, ids=lambda r: str(r.value)
)
def test_a_declared_short_answer_passes_on_the_declared_shape(
    fixture: Path, finish_reason: EnumProviderFinishReason
) -> None:
    """The recorded answer passes through ``short_form_adequacy``.

    It passes with the provider reporting nothing (``absent``) just as with
    ``stop``: the verdict rests on the declaration, not on the signal.
    """
    result = delta(_replayed_gate_input(fixture), finish_reason=finish_reason)
    assert result.passed, result.failure_reasons
    assert result.fail_category == "pass"
    assert result.quality_score >= _load(fixture)["request"]["required_bar"]
    rules = {evaluation.rule: evaluation for evaluation in result.rule_evaluations}
    assert rules["short_form_adequacy"].passed
    assert "semantic_adequacy" not in rules


@pytest.mark.parametrize(
    "prompt",
    [
        "say ok",
        "Say ok.",
        "say READY",
        'say "ok"',
        "  Say hello!  ",
        "Just say yes",
    ],
)
def test_a_bare_say_word_prompt_declares_an_exact_literal(prompt: str) -> None:
    """``say <word>`` and nothing else names the literal the answer must be."""
    assert (
        resolve_requested_shape_for_prompt(prompt)
        is EnumRequestedResponseShape.EXACT_LITERAL
    )


@pytest.mark.parametrize(
    "prompt",
    [
        "say what the quality gate does and why",
        "Say ok if the tests pass, otherwise explain the failure.",
        "Explain why the model would say ok here.",
        "Please summarise the plan and say whether it is ready.",
        _PROSE_PROMPT,
    ],
)
def test_a_prompt_that_only_mentions_saying_declares_nothing(prompt: str) -> None:
    """The say-word directive is anchored to the whole prompt, not a substring."""
    assert (
        resolve_requested_shape_for_prompt(prompt)
        is EnumRequestedResponseShape.UNCONSTRAINED
    )


# ---------------------------------------------------------------------------
# AC2: ``finish_reason=stop`` is not evidence of completeness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("content", ["Done", "Yes", "Sure", "ok"])
@pytest.mark.parametrize(
    "finish_reason", _NON_LENGTH_FINISH_REASONS, ids=lambda r: str(r.value)
)
def test_a_one_word_reply_to_a_prose_request_is_refused(
    content: str, finish_reason: EnumProviderFinishReason
) -> None:
    """The F2 negative fixture: no finish reason turns one word into a document."""
    result = delta(_prose_gate_input(content), finish_reason=finish_reason)
    assert not result.passed
    assert _refused_by_single_word_rule(result.failure_reasons), result.failure_reasons
    assert result.quality_score < 1.0


@pytest.mark.parametrize("fixture", [_SAY_OK, _READY], ids=["say_ok", "READY"])
def test_stop_does_not_lift_the_single_word_rule_on_the_class_band(
    fixture: Path,
) -> None:
    """With the recorded class band, ``stop`` changes nothing: still refused."""
    result = delta(
        _recorded_gate_input(fixture),
        finish_reason=EnumProviderFinishReason.STOP,
    )
    assert not result.passed
    assert _refused_by_single_word_rule(result.failure_reasons)
    assert all(reason.startswith(_SHAPE_REFUSED) for reason in result.failure_reasons)


# ---------------------------------------------------------------------------
# What a declared short shape does NOT excuse
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "expected_fragment"),
    [
        ("   ", "empty"),
        ("The change adds a", "truncated mid-clause"),
        ("The result is (", "truncated mid-token"),
    ],
    ids=["empty", "dangling-clause", "mid-token"],
)
def test_a_declared_shape_does_not_excuse_a_response_cut_short(
    content: str, expected_fragment: str
) -> None:
    """Only the single-word rule is dropped; the truncation rules stay."""
    result = delta(
        _contract_gate_input("document", "say ok", content),
        finish_reason=EnumProviderFinishReason.STOP,
    )
    assert not result.passed
    assert any(expected_fragment in reason for reason in result.failure_reasons), (
        result.failure_reasons
    )


def test_a_declared_shape_does_not_excuse_a_refusal() -> None:
    """A one-line refusal is still a refusal, whatever the prompt declared."""
    result = delta(
        _contract_gate_input("document", "say ok", "I cannot help with that."),
        finish_reason=EnumProviderFinishReason.STOP,
    )
    assert not result.passed


# ---------------------------------------------------------------------------
# The OMN-18278 set: the truncation veto is untouched
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("content", ["ok", "READY", "The gate scores it."])
def test_a_length_finish_is_still_vetoed(content: str) -> None:
    """``length`` vetoes before any content rule, one word or a sentence."""
    result = delta(
        _contract_gate_input("document", "say ok", content),
        finish_reason=EnumProviderFinishReason.LENGTH,
    )
    assert not result.passed
    assert result.fail_category == "fail_deterministic"
    assert "finish_reason=length" in result.failure_reasons[0]
    assert result.fallback_recommended


def test_the_result_records_the_signal_it_was_judged_with() -> None:
    """The verdict records the finish reason it saw, even where it decided nothing."""
    result = delta(
        _replayed_gate_input(_SAY_OK), finish_reason=EnumProviderFinishReason.STOP
    )
    assert result.finish_reason is EnumProviderFinishReason.STOP
