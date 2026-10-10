# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_operator_capture_compute classify: this week's operator messages (OMN-20905).

The fixture holds real operator messages from the week of 2026-10-05 and the delegated local
model's recorded answers to them (``onex delegate``, recorded 2026-10-10), so the test proves
the handler accepts the model's real output and maps each message to the kinds a reader expects.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.models.operator_capture import (
    EnumClassifierSource,
    EnumPromptOrigin,
    EnumUtteranceKind,
    ModelUtterance,
    ModelUtteranceClassification,
    ModelUtteranceClassifyRequest,
)
from omnimarket.nodes.node_operator_capture_compute.handlers.handler_utterance_classify import (
    HandlerUtteranceClassify,
    build_classify_prompt,
    heuristic_kind,
    is_machine_injection,
)

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).parent / "fixtures" / "week_2026_10_05_samples.json"
SAMPLES: list[dict[str, str]] = json.loads(FIXTURE.read_text())

K = EnumUtteranceKind
# What a reader expects each message to yield at least once (and never, for "thanks").
EXPECTED: dict[str, set[EnumUtteranceKind]] = {
    "m4-scope": {K.DECISION},
    "park-default": {K.DECISION},
    "access-mode": {K.DECISION, K.PREFERENCE},
    "bare-ids": {K.PREFERENCE},
    "capture-ruling": {K.PREFERENCE},
    "skills-question": {K.STATUS_QUESTION},
    "thanks": set(),
}


def _utterance(text: str, capture_id: str = "c1") -> ModelUtterance:
    return ModelUtterance(
        capture_id=capture_id,
        text=text,
        session_id="s-1",
        source="claude-code:remote",
        captured_at=datetime(2026, 10, 10, 17, 0, tzinfo=UTC),
    )


def _classify(
    text: str, response: str | None, model: str | None = "Qwen3.8-27B"
) -> ModelUtteranceClassification:
    return HandlerUtteranceClassify().handle(
        ModelUtteranceClassifyRequest(
            utterance=_utterance(text),
            delegated_response=response,
            delegated_model=model if response is not None else None,
            delegated_error=None if response is not None else "model down",
        )
    )


@pytest.mark.parametrize("sample", SAMPLES, ids=[s["id"] for s in SAMPLES])
def test_recorded_model_answers_are_accepted_and_map_to_expected_kinds(
    sample: dict[str, str],
) -> None:
    result = _classify(
        sample["text"], sample["delegated_response"], sample["delegated_model"]
    )
    assert result.origin is EnumPromptOrigin.OPERATOR
    assert result.classifier is EnumClassifierSource.DELEGATED, result.reason
    kinds = {i.kind for i in result.items}
    expected = EXPECTED[sample["id"]]
    if expected:
        assert expected & kinds, (sample["id"], kinds)
    else:
        assert kinds == set()
    for item in result.items:
        assert item.quote in sample["text"]
        assert item.kind is not K.CHAT


@pytest.mark.parametrize("sample", SAMPLES, ids=[s["id"] for s in SAMPLES])
def test_fallback_without_the_model_still_records_every_non_chat_sample(
    sample: dict[str, str],
) -> None:
    result = _classify(sample["text"], None)
    assert result.classifier is EnumClassifierSource.HEURISTIC
    assert result.reason == "fallback: model down"
    if EXPECTED[sample["id"]]:
        assert result.items, sample["id"]
    else:
        assert result.items == ()
    for item in result.items:
        assert item.quote in sample["text"]


def test_a_quote_that_is_not_in_the_message_discards_the_whole_answer() -> None:
    text = "Ship the capture node today. Also check the lab."
    answer = json.dumps(
        {
            "items": [
                {
                    "kind": "ask",
                    "quote": "Ship the capture node today.",
                    "subject": "ship",
                    "confidence": 0.9,
                },
                {
                    "kind": "ask",
                    "quote": "Check the lab please",
                    "subject": "lab",
                    "confidence": 0.9,
                },
            ]
        }
    )
    result = _classify(text, answer)
    assert result.classifier is EnumClassifierSource.HEURISTIC
    assert "not an exact substring" in result.reason
    assert {i.quote for i in result.items} == {
        "Ship the capture node today.",
        "Also check the lab.",
    }


@pytest.mark.parametrize(
    "answer",
    [
        "not json",
        json.dumps([]),
        json.dumps({"items": [], "extra": 1}),
        json.dumps(
            {
                "items": [
                    {"kind": "wish", "quote": "x y", "subject": "s", "confidence": 1}
                ]
            }
        ),
        json.dumps(
            {
                "items": [
                    {
                        "kind": "ask",
                        "quote": "Fix it now",
                        "subject": "s",
                        "confidence": 2,
                    }
                ]
            }
        ),
        json.dumps({"items": [{"kind": "ask", "quote": "Fix it now", "subject": "s"}]}),
    ],
)
def test_malformed_answers_fall_back(answer: str) -> None:
    result = _classify("Fix it now", answer)
    assert result.classifier is EnumClassifierSource.HEURISTIC
    assert [i.kind for i in result.items] == [K.ASK]


def test_fenced_json_answer_is_read() -> None:
    body = json.dumps(
        {
            "items": [
                {
                    "kind": "decision",
                    "quote": "Go with option B.",
                    "subject": "option b",
                    "confidence": 0.8,
                }
            ]
        }
    )
    result = _classify("Go with option B.", f"```json\n{body}\n```")
    assert result.classifier is EnumClassifierSource.DELEGATED
    assert [i.kind for i in result.items] == [K.DECISION]


@pytest.mark.parametrize(
    "text",
    [
        "<task-notification>\n<task-id>abc</task-id>",
        "[Workflow harness — user request] relayed",
        "<command-name>/clear</command-name>",
        "<system-reminder>x</system-reminder>",
        "Caveat: The messages below were generated by the user while running local commands.",
        "   ",
    ],
)
def test_machine_injections_yield_nothing(text: str) -> None:
    assert is_machine_injection(text)
    result = _classify(text, None)
    assert result.origin is EnumPromptOrigin.MACHINE
    assert result.items == ()


def test_drop_command_is_returned() -> None:
    result = _classify("drop ask-0123456789 and done ASK-abcdefabcd", None)
    assert result.drops == ("ask-0123456789", "ask-abcdefabcd")


@pytest.mark.parametrize(
    ("sentence", "kind"),
    [
        ("Can you check why the h201 floor is stale?", K.ASK),
        ("What is the state of the landing controller?", K.STATUS_QUESTION),
        ("From now on, never push me routine status.", K.PREFERENCE),
        ("Maybe we could try PolyStrata after M4.", K.IDEA),
        ("Agree with number two.", K.DECISION),
        ("Fix the dashboard projection.", K.ASK),
        ("That was a long day.", K.CHAT),
    ],
)
def test_heuristic_kinds(sentence: str, kind: EnumUtteranceKind) -> None:
    assert heuristic_kind(sentence) is kind


def test_prompt_carries_the_message_verbatim() -> None:
    text = 'line one\nline "two" | three'
    assert build_classify_prompt(text).endswith(f"MESSAGE:\n{text}\n")


def test_a_leading_again_does_not_hide_a_status_question() -> None:
    assert heuristic_kind("Again, why are we polling GitHub?") is K.STATUS_QUESTION
