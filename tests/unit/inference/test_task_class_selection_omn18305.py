# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Contract-declared task-class selection predicates (OMN-18305).

THE DEFECT. `onex delegate` chose a task class with a hardcoded keyword table
lifted from retired skill markdown — bare substring matching, first rule wins,
`test` rule first. On 2026-09-13 the committed 56,593-byte engineering standup
fixture classified as `test`, because the coordination-ledger rows it
summarises contain the word "test" 46 times; and `"the latest window"`
classified as `test` because "latest" contains "test". `test` is not one of the
classes whose quality bar arms `identifiers_grounded`, so the OMN-18297
grounding check and the prose quality band never ran on the answer.

Selection is now declared here, per class, and read by the consumer.

WHY THE EVALUATOR IS NOT TESTED HERE. It lives in `omnibase_infra`, because
repo layering runs compat -> core -> spi -> infra and omnimarket depends on
omnibase_infra, never the reverse — the CLI cannot import this package, so the
declaration is ours and the evaluation is its. omnimarket pins omnibase_infra
from the registry, so the new module is not importable here until a release.

What IS tested here is stronger than one worked example: the CONTRACT-level
properties that make the fixture's result a property of the contract rather
than a lucky phrase match. Those need no evaluator — they are arithmetic over
the declared predicates.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from omnimarket.inference.task_class_authority import (
    EnumGatewayExposure,
    load_task_class_authority,
)

pytestmark = pytest.mark.unit

_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "delegation"
    / "omn18297"
    / "d715f096_grounding_source.txt"
)

#: The classes whose `definition_of_done.heuristic` arms `identifiers_grounded`
#: — the OMN-18297 prose checks. Derived from the contract itself below, never
#: trusted as a list.
_GROUNDING_RULE = "identifiers_grounded"

#: The contract's public projection. THE OTHER HALF OF THIS ASSERTION LIVES IN
#: OMNIBASE_INFRA: `tests/unit/cli/test_cli_delegate.py::TestTaskTypeVocabulary`
#: pins `cli_delegate.TASK_TYPE_CHOICES` to this same list. Neither suite can
#: import the other's half (layering one way, a registry pin the other, and
#: omnibase_infra's venv-purity gate refuses to run at all with omnimarket
#: installed), so the seam is two pinned halves. A class added or removed here
#: turns this test red first; updating the CLI mirror is the fix.
_EXPECTED_PUBLIC_CLASSES = (
    "code_generation",
    "code_review",
    "complex_reasoning",
    "document",
    "planning",
    "reasoning",
    "refactor",
    "research",
    "review",
    "summarization",
    "test",
)


def _armed_classes() -> frozenset[str]:
    """Return the classes whose quality bar arms the identifier-grounding check."""
    authority = load_task_class_authority()
    armed: set[str] = set()
    for name, entry in authority.task_classes.items():
        dod = entry.model_extra.get("definition_of_done") if entry.model_extra else None
        heuristics = dod.get("heuristic") if isinstance(dod, dict) else None
        if isinstance(heuristics, list) and _GROUNDING_RULE in heuristics:
            armed.add(name)
    return frozenset(armed)


class TestEveryClassDeclaresItsPredicate:
    def test_the_contract_loads_with_a_selection_on_every_class(self) -> None:
        """``selection`` is a required field, so loading at all proves this."""
        authority = load_task_class_authority()
        assert len(authority.task_classes) == 15
        assert all(
            entry.selection is not None for entry in authority.task_classes.values()
        )

    def test_the_public_projection_is_the_cli_vocabulary(self) -> None:
        authority = load_task_class_authority()
        assert sorted(authority.public_task_classes) == sorted(_EXPECTED_PUBLIC_CLASSES)

    def test_internal_classes_are_never_selected_from_a_prompt(self) -> None:
        authority = load_task_class_authority()
        for name, entry in authority.task_classes.items():
            if entry.gateway_exposure is EnumGatewayExposure.INTERNAL:
                assert entry.selection.phrases == (), name


class TestShapeGatesTheKeyword:
    """The property that makes the fixture's result structural, not lucky."""

    def test_no_long_prose_prompt_can_reach_a_class_with_the_checks_disarmed(
        self,
    ) -> None:
        authority = load_task_class_authority()
        armed = _armed_classes()
        assert armed, "the contract arms identifier grounding on no class at all"

        reachable = {
            name
            for name in authority.public_task_classes
            if (max_words := authority.task_classes[name].selection.max_words) is None
            or max_words >= 5_000
        }
        assert reachable <= armed, sorted(reachable - armed)

    def test_the_keyword_classes_are_bounded(self) -> None:
        """`test` must not be eligible for a document-length prompt."""
        authority = load_task_class_authority()
        for name in ("test", "code_generation", "refactor"):
            max_words = authority.task_classes[name].selection.max_words
            assert max_words is not None, name
            assert max_words < 5_000, name


class TestTheRecordedStandup:
    """The OMN-18305 reproduction, as a contract-level statement."""

    def test_the_fixture_is_long_prose(self) -> None:
        words = len(_FIXTURE.read_text(encoding="utf-8").split())
        assert words > 5_000, words

    def test_only_armed_classes_are_eligible_for_the_fixture(self) -> None:
        """Shape gate first, then phrases — the contract's own evaluation order.

        `test` and `refactor` phrases DO occur in these 7,496 words, which is
        exactly the defect: a keyword present in the material being summarised
        is not a request to write a test. Their declared `max_words` makes them
        ineligible before any phrase is considered, so what remains is armed.
        """
        authority = load_task_class_authority()
        text = _FIXTURE.read_text(encoding="utf-8")
        lowered = text.lower()
        word_count = len(text.split())

        def eligible(name: str) -> bool:
            selection = authority.task_classes[name].selection
            if selection.min_words is not None and word_count < selection.min_words:
                return False
            if selection.max_words is not None and word_count > selection.max_words:
                return False
            return any(
                re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", lowered)
                for phrase in selection.phrases
            )

        claimants = {name for name in authority.public_task_classes if eligible(name)}
        assert claimants, "no declared predicate claims the recorded standup"
        assert claimants <= _armed_classes(), sorted(claimants - _armed_classes())
        # The keyword classes really are claimed on phrases, and really are
        # excluded on shape — a positive control on the paragraph above.
        phrase_only = {
            name
            for name in ("test", "refactor")
            if any(
                re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", lowered)
                for phrase in authority.task_classes[name].selection.phrases
            )
        }
        assert phrase_only, "the shape gate is doing no work on this fixture"
        assert not (phrase_only & claimants)

    def test_latest_does_not_match_the_test_class(self) -> None:
        """The four-word reproduction, at the predicate level."""
        authority = load_task_class_authority()
        for phrase in authority.task_classes["test"].selection.phrases:
            assert not re.search(
                rf"(?<!\w){re.escape(phrase)}(?!\w)", "the latest window"
            ), phrase

    def test_a_genuine_test_request_still_matches_the_test_class(self) -> None:
        authority = load_task_class_authority()
        assert any(
            re.search(
                rf"(?<!\w){re.escape(phrase)}(?!\w)",
                "write pytest cases for this function",
            )
            for phrase in authority.task_classes["test"].selection.phrases
        )
