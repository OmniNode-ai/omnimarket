# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""A prose enumeration is not a code request (OMN-19017, classifier half).

THE DEFECT, measured across FOUR lanes inside one hour. A prompt opening
``Enumerate test cases for one rule...`` asks for a list described IN WORDS.
The bare phrase ``test cases`` claimed it for the ``test`` class at priority
40, and ``test`` is graded by ``deterministic_acceptance``: the response must
compile as Python and carry no prose outside a code block. A prose answer can
never satisfy that, so every local rung was refused identically and
deterministically, and the ladder was guaranteed to exhaust.

From correlation ``04ac362b-8d9d-4208-b251-73a9a090d2a7``::

    rung 0  local        0.6  TASK_MISMATCH: non-artifact prose outside code block
    rung 1  local        0.6  MALFORMED: does not compile as Python: '→' (U+2192)
    rung 2  local        0.6  MALFORMED: leading zeros in decimal integer literals
    rung 3  cheap_cloud  0.0  provider HTTP 429 Too Many Requests

Also ``e811f3e5-1ccf-42be-9d88-707ed1923430``, ``4392a792`` and
``72593bc3-7dd5-4491-9d1f-f3bfe6ccf8a9``, all four resolving ``task_type:
test``.

THE ISOLATING CONTROL IS THE TICKET'S, not one written here. The same lane ran
the same task shape successfully as ``dd58507f-892a-418a-b8fe-f887495e4120``,
accepted on the FIRST rung at 1.0, differing only by an explicit
``--task-type document``. Same model, same endpoint, same hour, opposite
outcome. That rules out the model, the prompt length and the provider, and
leaves the classifier as the variable.

THE FIX is the OMN-18831 pattern, applied to the phrase that caused this one.
``test case`` and ``test cases`` move from unconditional phrases to
``qualified_phrases``, so they claim a prompt only with testing or code
vocabulary within the declared window. A positive qualifier vocabulary rather
than a list of prose disqualifiers, for the reason recorded on
``ModelQualifiedPhrases`` and already settled by OMN-18831. Deleting the
phrases was rejected there and is rejected here: ``add test cases to the
pytest module`` is a real code request and must keep arriving at this class.

The bare singular ``test`` also leaves the qualifier list, because a qualifier
sitting inside the phrase it qualifies can never refuse it. That is not a
detail noticed in review: the OMN-18831 invariant
``test_every_gated_phrase_has_a_qualifier_that_is_not_itself`` caught it, and
the entry below records the second reason, which is that an incidental nearby
``test`` would otherwise qualify a prose request and reintroduce the defect.

WHAT THIS MODULE DOES NOT CLAIM. Scope 2 of the ticket, the terminal naming
the rung that decided rather than the last rung's transport error, is NOT
here. It needs a gate-decided terminal cause, which merged to the core library
but is not yet released or pinned, so the truthful value does not exist to
assign. Asserting it now would be asserting a value nothing can produce.
"""

from __future__ import annotations

import pytest

from omnimarket.inference.task_class_authority import load_task_class_authority

pytestmark = pytest.mark.unit


# The two captured prompts, verbatim from their receipts rather than
# paraphrased, so the fixture is the request that actually occurred.
PROMPT_04AC362B = (
    "Enumerate test cases for one rule. A CI receipt may be re-emitted only "
    "when every one of its failing checks is binding-class, meaning the run "
    "could not bind its observation to the thing under test rather than the "
    "thing under test being unhealthy. Binding-class: convergence "
    "unestablished, probe read a different container generation. Not "
    "binding-class: readiness failed, migrations missing, consumer lag over "
    "bound. List the test cases that distinguish these two, including the "
    "ones most likely to be forgotten."
)

PROMPT_E811F5E5 = (
    "Enumerate RED test cases for a stale-cache defect in a Kafka deploy "
    "agent.\n\nContext: a Python agent has one serial loop: "
    "poll_and_accept() then execute_command() (20-40 min). Lag sampling "
    "happens only inside poll_and_accept."
)


def _test_selection() -> object:
    return load_task_class_authority().task_classes["test"].selection


def _matches(phrase: str, text: str) -> bool:
    """The evaluator's word-boundary rule, restated so this suite needs no import.

    Duplicated from the OMN-18831 module for the same reason recorded there:
    the evaluator lives in another repository and the version installed in
    this suite may predate the feature, so the contract half asserts over the
    contract rather than over an import it cannot make.
    """
    import re

    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text.lower()) is not None


# ---------------------------------------------------------------------------
# The contract statement of the fix.
# ---------------------------------------------------------------------------


def test_test_case_is_no_longer_an_unconditional_phrase() -> None:
    """RED: both phrases were unconditional, at priority 40, over a code floor."""
    selection = _test_selection()

    assert "test case" not in selection.phrases
    assert "test cases" not in selection.phrases
    assert selection.qualified_phrases is not None
    assert {"test case", "test cases"} <= set(selection.qualified_phrases.phrases)


def test_the_unambiguous_test_phrases_stay_unconditional() -> None:
    """The fix gates two phrases, not the class.

    Each of these names a test artifact outright and is never an ordinary
    English way to ask for prose, so gating them would only lose recall. This
    is the guard against a fix that quietly turned the whole class off.
    """
    phrases = set(_test_selection().phrases)

    for phrase in (
        "write a test",
        "write tests",
        "unit test",
        "unit tests",
        "pytest",
        "add a test",
        "add tests",
        "assertionerror",
        "failing test",
        "test failure",
        "traceback",
    ):
        assert phrase in phrases, phrase


def test_the_bare_singular_test_is_not_a_qualifier() -> None:
    """A qualifier inside the phrase it qualifies can never refuse it.

    Asserted here as well as by the OMN-18831 invariant, because the invariant
    states the rule generically and this states why THIS entry left the list:
    with ``test case`` gated, an incidental nearby ``test`` would qualify a
    prose request and reintroduce the defect.
    """
    gated = _test_selection().qualified_phrases

    assert gated is not None
    assert "test" not in gated.qualifiers
    # The plural and the tool names remain, so genuine code uses still qualify.
    assert {"tests", "testing", "unittest", "pytest"} <= set(gated.qualifiers)


# ---------------------------------------------------------------------------
# The reproduction, as arithmetic over the declared predicates.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "prompt"),
    [
        pytest.param("04ac362b", PROMPT_04AC362B, id="04ac362b"),
        pytest.param("e811f3e5", PROMPT_E811F5E5, id="e811f3e5"),
    ],
)
def test_no_unconditional_phrase_of_a_code_class_claims_the_captured_prompt(
    label: str, prompt: str
) -> None:
    """RED against both captured prompts: ``test cases`` claimed them outright.

    Asserted over the UNCONDITIONAL phrases of the two code-floored classes,
    which is the claim that actually changed. What a gated phrase does with a
    given prompt depends on the evaluator's window arithmetic and is asserted
    end to end in the other repository's falsifier table; what this module can
    state without that import is that no phrase claims these prompts
    unconditionally any more.
    """
    authority = load_task_class_authority()

    for class_name in ("test", "code_generation"):
        selection = authority.task_classes[class_name].selection
        claiming = [phrase for phrase in selection.phrases if _matches(phrase, prompt)]
        assert not claiming, f"{class_name} still claims {label} on {claiming}"


@pytest.mark.parametrize(
    ("label", "prompt"),
    [
        pytest.param("04ac362b", PROMPT_04AC362B, id="04ac362b"),
        pytest.param("e811f3e5", PROMPT_E811F5E5, id="e811f3e5"),
    ],
)
def test_the_gated_phrase_does_still_occur_in_the_captured_prompt(
    label: str, prompt: str
) -> None:
    """Positive control: the phrase is present, so the gate is doing the work.

    Without this, the assertion above would also pass if the phrase had simply
    been deleted from the contract, or if the captured prompt had been quietly
    reworded into one that never matched.
    """
    gated = _test_selection().qualified_phrases

    assert gated is not None
    assert [phrase for phrase in gated.phrases if _matches(phrase, prompt)] == [
        "test cases"
    ]


def test_a_genuine_code_request_is_still_claimed_unconditionally() -> None:
    """AC3: the selector is not simply loosened.

    A code request that names a test artifact outright must still be claimed
    without needing a qualifier at all, which is what keeps this a narrowing
    of one ambiguous phrase rather than a weakening of the class.
    """
    selection = _test_selection()
    prompt = "write a test for the parser that rejects a trailing comma."

    assert [phrase for phrase in selection.phrases if _matches(phrase, prompt)] == [
        "write a test"
    ]
