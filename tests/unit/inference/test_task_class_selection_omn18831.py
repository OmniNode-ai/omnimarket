# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Qualifier-gated selection phrases, the contract half (OMN-18831).

THE DEFECT. Two declared phrases are ordinary English before they are
technical, and both classes they belong to grade their answers on a
DETERMINISTIC floor rather than a prose one.

* `code_generation` declared `"write a"`. A 388-word prose request opening
  `"Write a GitHub PR body in markdown from these facts."` was claimed on that
  two-word match at priority 30, and the `compiles_without_errors` floor then
  refused five correct English answers in a row at 0.433 against a bar of
  0.850 -- three local rungs and two cloud rungs, all identical. Run
  `21b33edf-32aa-4279-9c1b-52b034c2ee9e`, correlation
  `13028f3f-0dc3-4eec-8773-78a845696e01`, 2026-09-19T14:28:31Z. The same facts
  reworded to avoid those two words classified as `document` and passed on the
  first local rung (run `06f6b56a-8ded-4594-bf19-70c32fcc43ca`).
* `test` declared `"assertion"` and `"assertions"`, the ordinary English word
  for a claim, and `test` is graded by `deterministic_acceptance`.

THE FIX. Those phrases moved to `qualified_phrases`, where they claim a prompt
only with one of the class's declared code or testing terms within
`within_words` words. See `ModelQualifiedPhrases` for why a positive qualifier
vocabulary was chosen over a list of prose disqualifiers, and why deleting the
phrases was rejected.

THE ROUTING TABLE. The evaluator lives beside this contract, in
`ModelTaskClassAuthority.resolve_task_type` (OMN-19407), so the end-to-end
falsifier table runs against the LIVE contract in
`test_task_class_resolution_omn19407.py`. There is no mirror to keep in step.
"""

from __future__ import annotations

import re

import pytest

from omnimarket.inference.task_class_authority import (
    EnumGatewayExposure,
    load_task_class_authority,
)

pytestmark = pytest.mark.unit


def _matches(phrase: str, text: str) -> bool:
    """The evaluator's word-boundary rule, restated so this suite needs no import."""
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text) is not None


class TestTheAmbiguousPhrasesAreGated:
    """The contract-level statement of the fix, independent of any evaluator."""

    def test_write_a_is_no_longer_an_unconditional_code_generation_phrase(
        self,
    ) -> None:
        selection = (
            load_task_class_authority().task_classes["code_generation"].selection
        )
        assert "write a" not in selection.phrases
        assert selection.qualified_phrases is not None
        assert "write a" in selection.qualified_phrases.phrases

    def test_assertion_is_no_longer_an_unconditional_test_phrase(self) -> None:
        selection = load_task_class_authority().task_classes["test"].selection
        assert "assertion" not in selection.phrases
        assert "assertions" not in selection.phrases
        assert selection.qualified_phrases is not None
        assert {"assertion", "assertions"} <= set(selection.qualified_phrases.phrases)

        # OMN-19017 widened this block with "test case"/"test cases" for the
        # same reason. The assertion above was an EQUALITY on the gated set,
        # which made it a pin on the whole block rather than on the two
        # phrases this test is about; a later ticket adding a phrase for the
        # identical reason should not have to edit an OMN-18831 test to say
        # so. It is now a containment, and the full membership of the block is
        # pinned by the projection digest above, which is the assertion that
        # exists to notice a contract edit.

    def test_assertionerror_stays_unqualified(self) -> None:
        """It is never ordinary English, so gating it would only lose recall."""
        selection = load_task_class_authority().task_classes["test"].selection
        assert "assertionerror" in selection.phrases

    def test_no_surviving_plain_phrase_matches_the_recorded_prose_request(
        self,
    ) -> None:
        """The reproduction, as arithmetic over the declared predicates.

        The prompt is 388 words, so every class's shape gate admits it except
        the ones with a `min_words` above that; what is asserted is the
        stronger property -- that no DETERMINISTIC-acceptance class claims it
        on a plain phrase any more.
        """
        authority = load_task_class_authority()
        prompt = (
            "write a github pr body in markdown from these facts. "
            "no preamble, no commentary, output only the body."
        )
        for name in ("code_generation", "test", "refactor"):
            claiming = [
                phrase
                for phrase in authority.task_classes[name].selection.phrases
                if _matches(phrase, prompt)
            ]
            assert not claiming, f"{name} still claims the prose request on {claiming}"

    def test_the_gated_phrases_do_still_occur_in_the_recorded_request(self) -> None:
        """Positive control: the phrase is present, so the gate is doing the work.

        Without this, the assertion above would also pass if the phrase had
        simply been deleted, or if the prompt had been quietly reworded.
        """
        authority = load_task_class_authority()
        prompt = "write a github pr body in markdown from these facts."
        gated = authority.task_classes["code_generation"].selection.qualified_phrases
        assert gated is not None
        assert [phrase for phrase in gated.phrases if _matches(phrase, prompt)] == [
            "write a"
        ]

    def test_every_gated_phrase_has_a_qualifier_that_is_not_itself(self) -> None:
        """A phrase that contains its own qualifier could never be refused."""
        authority = load_task_class_authority()
        for name, entry in authority.task_classes.items():
            gated = entry.selection.qualified_phrases
            if gated is None:
                continue
            for phrase in gated.phrases:
                self_qualifying = [
                    qualifier
                    for qualifier in gated.qualifiers
                    if _matches(qualifier, phrase)
                ]
                assert not self_qualifying, (name, phrase, self_qualifying)

    def test_a_gated_phrase_is_never_also_a_plain_phrase_of_the_same_class(
        self,
    ) -> None:
        """Declaring it both ways would leave the class ungated and look fixed."""
        authority = load_task_class_authority()
        for name, entry in authority.task_classes.items():
            gated = entry.selection.qualified_phrases
            if gated is None:
                continue
            overlap = set(gated.phrases) & set(entry.selection.phrases)
            assert not overlap, (name, sorted(overlap))

    def test_internal_classes_declare_no_gated_phrases_either(self) -> None:
        """`phrases == ()` already says 'never selected'; so must the new field."""
        authority = load_task_class_authority()
        for name, entry in authority.task_classes.items():
            if entry.gateway_exposure is EnumGatewayExposure.INTERNAL:
                assert entry.selection.qualified_phrases is None, name


class TestTheGenuineRequestsAreStillClaimable:
    """Positive controls at the contract level: deletion would fail these."""

    @pytest.mark.parametrize(
        ("prompt", "class_name", "expected_phrase"),
        [
            (
                "write a parser for the lane manifest file.",
                "code_generation",
                "write a",
            ),
            (
                "build a cli subcommand that drains the queue.",
                "code_generation",
                "build a",
            ),
            ("add assertions to the auth tests.", "test", "assertions"),
        ],
    )
    def test_the_phrase_and_a_qualifier_both_occur(
        self, prompt: str, class_name: str, expected_phrase: str
    ) -> None:
        gated = (
            load_task_class_authority()
            .task_classes[class_name]
            .selection.qualified_phrases
        )
        assert gated is not None
        assert _matches(expected_phrase, prompt)
        assert any(_matches(qualifier, prompt) for qualifier in gated.qualifiers)
