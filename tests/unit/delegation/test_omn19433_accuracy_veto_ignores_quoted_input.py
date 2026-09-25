# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-19433: the accuracy veto fires on the answer's own disclaimer, not on a quote.

The blocking rule ``accurate`` refuses a response that disclaims its own
accuracy ("this may be inaccurate", "I cannot verify ..."). It found those
phrases by substring anywhere in the answer, including text the answer copied
from its own input. Run ``01bd1d20`` rendered a report from a facts block in
which one row said "none is marked UNVERIFIED"; the report quoted that row, the
veto fired on the quoted word, and three local rungs were spent before the run
failed.

A phrase the answer quoted is not the answer disclaiming anything. The
distinction is made from the text, deterministically: an occurrence counts as
quoted when the phrase AND the words beside it appear together in the input
the response was derived from. A hedging phrase the input does not carry in
that context is still the answer's own, and still vetoes. With no input to
compare against, every occurrence vetoes, exactly as before.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models import (
    ModelQualityGateInput,
)

pytestmark = pytest.mark.unit

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "delegation" / "omn19433"
_SOURCE = (_FIXTURES / "01bd1d20_grounding_source_excerpt.txt").read_text()
_ANSWER = (_FIXTURES / "01bd1d20_response_excerpt.txt").read_text()

_CORRELATION_ID = UUID("9a4d6b8f-0132-4034-80da-1d2aa168e23b")

#: The ``document`` class DoD, as recorded on the run.
_DOCUMENT_DETERMINISTIC = ("response_non_empty",)
_DOCUMENT_HEURISTIC = ("no_refusal", "accurate", "semantic_adequacy")

_ACCURACY_VETO = "explicitly disclaims accuracy"


def _gate(answer: str, source: str | None) -> tuple[str, ...]:
    result = delta(
        ModelQualityGateInput(
            correlation_id=_CORRELATION_ID,
            task_type="document",
            llm_response_content=answer,
            dod_deterministic=_DOCUMENT_DETERMINISTIC,
            dod_heuristic=_DOCUMENT_HEURISTIC,
        ),
        grounding_source=source,
    )
    return result.failure_reasons


def _vetoed(reasons: tuple[str, ...]) -> bool:
    return any(_ACCURACY_VETO in reason for reason in reasons)


# ---------------------------------------------------------------------------
# AC1: the recorded run
# ---------------------------------------------------------------------------


def test_the_recorded_word_is_in_both_the_input_and_the_answer() -> None:
    """The premise: the answer's hedging word is one it copied from its input."""
    assert "unverified" in _SOURCE.lower()
    assert "unverified" in _ANSWER.lower()


def test_without_the_input_the_recorded_answer_is_vetoed() -> None:
    """Positive control on the fixture: the veto still sees the word."""
    assert _vetoed(_gate(_ANSWER, None))


def test_the_recorded_answer_is_not_vetoed_for_quoting_its_input() -> None:
    """AC1: replayed with its input, the recorded answer is not vetoed."""
    reasons = _gate(_ANSWER, _SOURCE)
    assert not _vetoed(reasons), reasons


# ---------------------------------------------------------------------------
# AC2: a first-person disclaimer still vetoes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "disclaimer",
    [
        "I cannot verify that the migration ran, so treat this as a guess.",
        "I could not check this myself and the figure may be inaccurate.",
        "I am not sure the count is right.",
        "I don't know whether the deploy finished.",
    ],
)
def test_the_answer_disclaiming_its_own_claim_is_still_vetoed(disclaimer: str) -> None:
    """AC2: the answer's own disclaimer vetoes, even with an input supplied."""
    answer = f"{_ANSWER}\nSummary: {disclaimer}\n"
    reasons = _gate(answer, _SOURCE)
    assert _vetoed(reasons), reasons


def test_the_word_in_the_input_does_not_excuse_the_answer_using_it_itself() -> None:
    """A word the input carries elsewhere does not license the answer's own hedge."""
    answer = f"{_ANSWER}\nOverall, this report is unverified and may be inaccurate.\n"
    reasons = _gate(answer, _SOURCE)
    assert _vetoed(reasons), reasons


# ---------------------------------------------------------------------------
# AC3: present in the input and quoted -> no veto; absent from the input -> veto
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "vetoed"),
    [
        ('{"status": "The deploy row is not verified yet."}', False),
        ('{"status": "The deploy row is pending."}', True),
    ],
    ids=["present-in-input", "absent-from-input"],
)
def test_a_quoted_hedge_passes_and_an_unsourced_one_does_not(
    source: str, vetoed: bool
) -> None:
    """AC3: the same answer text, judged against two inputs."""
    answer = "Status: The deploy row is not verified yet. Nothing else changed."
    assert _vetoed(_gate(answer, source)) is vetoed


def test_a_quote_is_matched_across_case_and_punctuation() -> None:
    """A quote need not keep the input's case or its JSON punctuation."""
    source = '{"reason": "none is marked UNVERIFIED"}'
    answer = "Reason: none is marked unverified."
    assert not _vetoed(_gate(answer, source))


def test_every_occurrence_must_be_a_quote() -> None:
    """One quoted occurrence does not excuse a second, unquoted one."""
    source = '{"reason": "none is marked UNVERIFIED"}'
    answer = "Reason: none is marked UNVERIFIED. My own total is unverified too."
    reasons = _gate(answer, source)
    assert _vetoed(reasons), reasons
    # The offset named is the unquoted occurrence, not the quoted one.
    veto = next(reason for reason in reasons if _ACCURACY_VETO in reason)
    assert f"unverified@offset={answer.lower().rfind('unverified')}" in veto
