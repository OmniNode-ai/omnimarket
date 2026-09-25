# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-13967: a complete short answer is not a fragment when the provider says it finished.

``semantic_adequacy`` refuses a bare single token with no terminal punctuation.
Two recorded dev-lane runs show what that rule costs:

* run ``1aeccaa6`` (``tests/fixtures/receipts/omn13967_say_ok_1aeccaa6.json``):
  the prompt was ``say ok``, the local model answered ``ok`` in 310 ms, and the
  gate refused it as a single-word fragment;
* correlation ``f037b9be`` (``tests/fixtures/receipts/omn19016_ready_probe_f037b9be.json``):
  the prompt asked for exactly one word, the model answered ``READY``, and four
  rungs refused it the same way.

Neither answer was cut short. The text alone cannot prove that, but the
provider can: ``finish_reason=stop`` is the provider stating that the model
emitted its own stop condition. When that signal is present the single-word
rule has nothing left to detect, so it stands aside. Every rule that detects a
response cut short -- empty, cut mid-token, cut mid-clause -- still applies,
and when the provider reports nothing (``absent``) or anything other than
``stop``, the single-word rule applies exactly as before.

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
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models import (
    ModelQualityGateInput,
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


def _load(path: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(path.read_text())
    return loaded


def _recorded_gate_input(path: Path) -> ModelQualityGateInput:
    request = _load(path)["request"]
    return ModelQualityGateInput(
        correlation_id=_CORRELATION_ID,
        task_type=request["task_type"],
        llm_response_content=request["llm_response_content"],
        dod_deterministic=tuple(request["dod_deterministic"]),
        dod_heuristic=tuple(request["dod_heuristic"]),
    )


def _document_input(content: str) -> ModelQualityGateInput:
    return _recorded_gate_input(_SAY_OK).model_copy(
        update={"llm_response_content": content}
    )


def _refused_by_single_word_rule(reasons: tuple[str, ...]) -> bool:
    return any(_SINGLE_WORD_RULE_TEXT in reason for reason in reasons)


# ---------------------------------------------------------------------------
# The recorded fixtures: the premise, then the fix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", [_SAY_OK, _READY], ids=["say_ok", "READY"])
def test_the_recorded_run_was_refused_by_the_single_word_rule(fixture: Path) -> None:
    """The premise, read off the recorded terminal rather than restated."""
    recorded = _load(fixture)
    failed = " ".join(attempt["error_message"] for attempt in recorded["attempts"])
    assert _SINGLE_WORD_RULE_TEXT in failed
    assert recorded["terminal"]["quality_gate_passed"] is False


@pytest.mark.parametrize("fixture", [_SAY_OK, _READY], ids=["say_ok", "READY"])
def test_a_short_answer_the_provider_finished_passes(fixture: Path) -> None:
    """The fix: ``finish_reason=stop`` makes the recorded answer acceptable."""
    result = delta(
        _recorded_gate_input(fixture),
        finish_reason=EnumProviderFinishReason.STOP,
    )
    assert result.passed, result.failure_reasons
    assert result.fail_category == "pass"
    assert result.quality_score >= _load(fixture)["request"]["required_bar"]
    rules = {evaluation.rule: evaluation for evaluation in result.rule_evaluations}
    assert rules["semantic_adequacy"].passed


@pytest.mark.parametrize("fixture", [_SAY_OK, _READY], ids=["say_ok", "READY"])
def test_without_the_provider_signal_the_old_rule_stands(fixture: Path) -> None:
    """``absent`` is not evidence of a finish, so nothing changes without it."""
    result = delta(
        _recorded_gate_input(fixture),
        finish_reason=EnumProviderFinishReason.ABSENT,
    )
    assert not result.passed
    assert _refused_by_single_word_rule(result.failure_reasons)
    assert all(reason.startswith(_SHAPE_REFUSED) for reason in result.failure_reasons)


@pytest.mark.parametrize(
    "finish_reason",
    [
        EnumProviderFinishReason.ABSENT,
        EnumProviderFinishReason.UNRECOGNISED,
        EnumProviderFinishReason.CONTENT_FILTER,
        EnumProviderFinishReason.TOOL_CALLS,
        EnumProviderFinishReason.FUNCTION_CALL,
    ],
)
def test_only_stop_lifts_the_single_word_rule(
    finish_reason: EnumProviderFinishReason,
) -> None:
    """Every finish reason that is not ``stop`` keeps the old verdict."""
    result = delta(_document_input("ok"), finish_reason=finish_reason)
    assert not result.passed
    assert _refused_by_single_word_rule(result.failure_reasons)


# ---------------------------------------------------------------------------
# What ``stop`` does NOT excuse
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "expected_fragment"),
    [
        ("   ", "empty"),
        ("The change adds a", "truncated mid-clause"),
        ("The result is (", "truncated mid-token"),
        ("the", "truncated mid-clause"),
    ],
    ids=["empty", "dangling-clause", "mid-token", "lone-function-word"],
)
def test_stop_does_not_excuse_a_response_cut_short(
    content: str, expected_fragment: str
) -> None:
    """The lifted rule is the single-word one only; the truncation rules stay."""
    result = delta(
        _document_input(content), finish_reason=EnumProviderFinishReason.STOP
    )
    assert not result.passed
    assert any(expected_fragment in reason for reason in result.failure_reasons), (
        result.failure_reasons
    )


def test_stop_does_not_excuse_a_refusal() -> None:
    """A one-line refusal is still a refusal, whatever the provider reports."""
    result = delta(
        _document_input("I cannot help with that."),
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
        _document_input(content), finish_reason=EnumProviderFinishReason.LENGTH
    )
    assert not result.passed
    assert result.fail_category == "fail_deterministic"
    assert "finish_reason=length" in result.failure_reasons[0]
    assert result.fallback_recommended


def test_the_result_records_the_signal_it_was_judged_with() -> None:
    """The verdict carries the finish reason that lifted the rule."""
    result = delta(_document_input("ok"), finish_reason=EnumProviderFinishReason.STOP)
    assert result.finish_reason is EnumProviderFinishReason.STOP
