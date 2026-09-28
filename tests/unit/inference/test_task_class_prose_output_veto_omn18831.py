# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The prose-output veto, the contract half (OMN-18831, the 2026-09-20 residual).

THE DEFECT THE QUALIFIER GATE COULD NOT REACH. OMN-18831's first fix gated the
ambiguous VERBS ("write a", "assertion") on a nearby code artifact. Its own
2026-09-20 comment recorded what that leaves open: an UNAMBIGUOUS test phrase
that appears because the prompt describes code work rather than asks for it.
Qualifying "unit tests" would not help, because the prompt genuinely mentions
unit tests; it just is not asking for one.

The live corpus on this host still carries one such run after every earlier
fix: ``1a67961a-664e-4a98-8680-378e8c393921`` asked for "a markdown table ...
for a pull request description. ... Output only the table, no prose", whose
first row counted "collectable pytest modules". The installed classifier still
resolves it to ``test`` on "pytest" (read 2026-09-23 against the dispatch venv,
omnibase_infra 0.38.57, omnimarket 0.4.211). Three local rungs returned the
table and were refused on the compilation floor at 0.333.

THE FIX. ``selection.vetoed_by`` names the REQUESTED OUTPUT, which the prompt
states outright. It is declared on every public class graded by
``deterministic_acceptance``. What this file pins is the contract-level
statement: which classes carry it, that they carry the same list, that no veto
can make its own class unreachable, and that the recorded prompt names a vetoed
artifact while the recorded genuine code request names none. The routing table
itself runs in omnibase_infra against the digest-pinned mirror.
"""

from __future__ import annotations

import re

import pytest

from omnimarket.inference.task_class_authority import (
    EnumGatewayExposure,
    ModelTaskClassSelection,
    load_task_class_authority,
)

pytestmark = pytest.mark.unit

#: Run 1a67961a-664e-4a98-8680-378e8c393921, the prompt verbatim from its
#: run.json. Classified ``test`` on "pytest"; three local rungs refused at 0.333.
_RECORDED_TABLE_REQUEST = (
    "Reformat these measured numbers into a markdown table of exactly four data "
    "rows plus a header, for a pull request description. Columns: Metric, "
    "Before, After, Delta. Output only the table, no prose. Numbers: collectable "
    "pytest modules summed over the last 20 merged pull requests, before 7014, "
    "after 7054, delta plus 40 which is plus 0.57 percent. Pull requests whose "
    "shard count changed, before 0, after 0, delta 0. Pull requests escalated to "
    "the whole suite, before 0, after 0, delta 0. Worst-case selector wall time "
    "in seconds, before 0.072, after 0.344, delta plus 0.272."
)

#: Run 771582cc-f1e0-4a17-84a3-94832ff70070, opening verbatim: a genuine code
#: request from the same corpus, which must keep reaching code_generation.
#: Note it says "no prose" -- an instruction ABOUT the answer's form that is
#: deliberately not a veto phrase, since a code answer carries no prose.
_RECORDED_CODE_REQUEST = (
    "Write a POSIX-bash function named parse_reset_epoch. Input: one string "
    "argument, a refusal banner line. Output: print epoch seconds of the reset "
    "moment on stdout and return 0, or print nothing and return 1 when no reset "
    "time can be found. Return only the bash function body, no prose, no "
    "markdown fence."
)


def _matches(phrase: str, text: str) -> bool:
    """The evaluator's word-boundary rule, restated so this suite needs no import."""
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text) is not None


def _deterministic_public_classes() -> dict[str, ModelTaskClassSelection]:
    authority = load_task_class_authority()
    raw_contract = authority.model_dump()["task_classes"]
    return {
        name: entry.selection
        for name, entry in authority.task_classes.items()
        if entry.gateway_exposure is EnumGatewayExposure.PUBLIC
        and raw_contract[name].get("score_source") == "deterministic_acceptance"
    }


class TestWhoCarriesTheVeto:
    def test_the_denominator_is_the_three_compilation_graded_public_classes(
        self,
    ) -> None:
        """Pinned by name so a fourth deterministic class cannot arrive unvetoed."""
        assert sorted(_deterministic_public_classes()) == [
            "code_generation",
            "refactor",
            "test",
        ]

    def test_every_compilation_graded_public_class_declares_the_veto(self) -> None:
        for name, selection in _deterministic_public_classes().items():
            assert selection.vetoed_by, name

    def test_they_all_declare_the_same_list(self) -> None:
        """One alias in the contract; three copies would drift."""
        lists = {
            name: selection.vetoed_by
            for name, selection in _deterministic_public_classes().items()
        }
        assert len(set(lists.values())) == 1, lists

    def test_no_prose_graded_class_declares_a_veto(self) -> None:
        """A veto on a prose class would push prose requests off prose."""
        authority = load_task_class_authority()
        deterministic = set(_deterministic_public_classes())
        for name, entry in authority.task_classes.items():
            if name in deterministic:
                continue
            assert entry.selection.vetoed_by == (), name


class TestNoVetoMakesItsOwnClassUnreachable:
    def test_no_veto_phrase_contains_or_is_contained_by_a_claiming_phrase(
        self,
    ) -> None:
        """A veto that fires on every prompt the class claims would disable it.

        That happens when a claiming phrase CONTAINS a veto: every prompt the
        phrase claims then also names the veto. The other direction is not a
        collision. A veto that contains a claiming phrase ("do not write code"
        contains "write", OMN-19523) is more specific than the claim, fires
        only on prompts that spell the whole veto out, and so refuses exactly
        the prompts it names and no others.
        """
        for name, selection in _deterministic_public_classes().items():
            claiming = list(selection.phrases)
            if selection.qualified_phrases is not None:
                claiming += list(selection.qualified_phrases.phrases)
            for veto in selection.vetoed_by:
                collisions = [phrase for phrase in claiming if _matches(veto, phrase)]
                assert not collisions, (name, veto, collisions)

    def test_a_claiming_phrase_that_contains_a_veto_is_still_caught(self) -> None:
        """Positive control for the narrowed check above: the real direction fires."""
        claiming = ("write a pr body for",)
        collisions = [
            phrase
            for phrase in claiming
            if any(_matches(veto, phrase) for veto in ("pr body",))
        ]
        assert collisions == ["write a pr body for"]

    def test_the_veto_is_lowercase_and_trimmed_at_load(self) -> None:
        with pytest.raises(ValueError, match="lowercase"):
            ModelTaskClassSelection(priority=1, phrases=(), vetoed_by=("PR body",))


class TestTheRecordedPrompts:
    def test_the_recorded_table_request_is_claimed_by_test_and_names_a_veto(
        self,
    ) -> None:
        """Both halves: the claim that caused the misroute, and the veto that ends it."""
        test_selection = _deterministic_public_classes()["test"]
        lowered = _RECORDED_TABLE_REQUEST.lower()
        assert _matches("pytest", lowered)
        assert "pytest" in test_selection.phrases
        vetoes = [v for v in test_selection.vetoed_by if _matches(v, lowered)]
        assert vetoes == ["pull request description"]

    def test_the_recorded_code_request_names_no_veto(self) -> None:
        """Positive control: the genuine request from the same corpus is untouched."""
        lowered = _RECORDED_CODE_REQUEST.lower()
        for name, selection in _deterministic_public_classes().items():
            fired = [v for v in selection.vetoed_by if _matches(v, lowered)]
            assert not fired, (name, fired)
