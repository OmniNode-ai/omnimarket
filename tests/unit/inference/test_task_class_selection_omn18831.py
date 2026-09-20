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

WHY THE ROUTING TABLE IS NOT HERE. The evaluator lives in `omnibase_infra` and
is pinned from the registry, so the version installed in this suite predates
the feature. The end-to-end falsifier table runs THERE, against a mirror of
this contract's selection projection. What is asserted here is that the mirror
is still this contract: both suites hash the same canonical projection to the
same constant, so a contract edit turns this test red and prints the digest to
carry across. That is the only seam available -- neither suite can import the
other's half.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
import yaml

import omnimarket
from omnimarket.inference.task_class_authority import (
    EnumGatewayExposure,
    load_task_class_authority,
)

pytestmark = pytest.mark.unit

#: Digest of the canonical public selection projection.
#:
#: THE OTHER HALF OF THIS ASSERTION LIVES IN OMNIBASE_INFRA:
#: `tests/unit/cli/test_task_class_selection_omn18831.py` hashes its production
#: mirror fixture to this same constant and runs the OMN-18831 falsifier table
#: against it. Editing any public class's `selection` block turns this test red
#: with the new digest in its message; regenerate the mirror there and update
#: both constants in the same change. A digest matching on one side only means
#: the falsifier table is being run against a contract that no longer exists.
PRODUCTION_SELECTION_DIGEST = (
    "00071489afc6ad44768687b8dd0d8c69d5e15baaf800562e888375aba868b089"
)


def _canonical_projection() -> str:
    """Return the canonical public selection projection of the live contract.

    Duplicated verbatim in omnibase_infra's half. The duplication is the point:
    a shared helper would have to live in one repo and be imported by the
    other, which is the import neither repo can make.
    """
    contract = (
        Path(omnimarket.__file__).resolve().parent
        / "configs"
        / "task_class_contracts.v1.yaml"
    )
    raw = yaml.safe_load(contract.read_text(encoding="utf-8"))
    projection: dict[str, object] = {}
    for name, entry in raw["task_classes"].items():
        if not isinstance(entry, dict) or entry.get("gateway_exposure") != "public":
            continue
        selection = entry["selection"]
        qualified = selection.get("qualified_phrases")
        projection[str(name)] = {
            "priority": int(selection["priority"]),
            "min_words": selection.get("min_words"),
            "max_words": selection.get("max_words"),
            "phrases": sorted(str(item) for item in (selection.get("phrases") or ())),
            "qualified_phrases": None
            if qualified is None
            else {
                "within_words": int(qualified["within_words"]),
                "phrases": sorted(str(item) for item in qualified["phrases"]),
                "qualifiers": sorted(str(item) for item in qualified["qualifiers"]),
            },
        }
    return json.dumps(projection, sort_keys=True, separators=(",", ":"))


def _matches(phrase: str, text: str) -> bool:
    """The evaluator's word-boundary rule, restated so this suite needs no import."""
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text) is not None


class TestTheMirrorSeam:
    def test_the_live_projection_hashes_to_the_pinned_digest(self) -> None:
        digest = hashlib.sha256(_canonical_projection().encode("utf-8")).hexdigest()
        assert digest == PRODUCTION_SELECTION_DIGEST, (
            "the selection projection changed; regenerate omnibase_infra's "
            "production mirror fixture and set both digests to "
            f"{digest}"
        )


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
        assert set(selection.qualified_phrases.phrases) == {"assertion", "assertions"}

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
