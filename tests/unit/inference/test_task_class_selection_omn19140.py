# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The summarization floor and the short-prompt opening, the contract half (OMN-19140).

THE DEFECT. ``summarization`` declares ``min_words: 120`` and the evaluator
applies a shape gate before any phrase, so "Summarize in one sentence: ..." was
structurally ineligible for the class that exists for it. The OMN-19136 shadow
run traced 15 of its 19 disagreements with the incumbent to that one line.

WHAT THE FLOOR WAS FOR, measured rather than assumed. Removing the floor over
every recorded delegation prompt moved 48 prompts to ``summarization``: the 44
claimed on the verb opened with it and asked for a summary, and the 4 claimed
on the noun ``summary`` asked for something else. So the floor stays, and a
``short_prompt`` block admits a shorter prompt only when it opens with an
imperative verb the class already claims.

WHY THE ROUTING TABLE IS NOT HERE. The evaluator lives in omnibase_infra and is
pinned from the registry. Its falsifier table replays the shadow corpus against
a mirror of this contract, and the mirror is held to this contract by the
digest in ``test_task_class_selection_omn18831.py``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.inference.task_class_authority import (
    ModelTaskClassSelection,
    load_task_class_authority,
)

pytestmark = pytest.mark.unit


def _summarization() -> ModelTaskClassSelection:
    return load_task_class_authority().task_classes["summarization"].selection


class TestTheFloorIsKept:
    def test_the_class_floor_is_still_120_words(self) -> None:
        """AC3: the floor is not deleted because it was in the way."""
        assert _summarization().min_words == 120

    def test_a_short_prompt_is_admitted_only_by_the_imperative_verbs(self) -> None:
        short = _summarization().short_prompt
        assert short is not None
        assert short.opening_phrases == ("summarise", "summarize")
        assert short.min_words == 6

    def test_the_ordinary_nouns_still_need_the_full_floor(self) -> None:
        """The phrases that mis-claimed short prompts when the floor was removed."""
        short = _summarization().short_prompt
        assert short is not None
        for noun in ("summary", "digest", "what happened", "write up", "stand up"):
            assert noun in _summarization().phrases
            assert noun not in short.opening_phrases

    def test_every_opening_phrase_is_also_a_plain_phrase(self) -> None:
        """AC2 structurally: the padded request is claimed by the same verb."""
        short = _summarization().short_prompt
        assert short is not None
        assert set(short.opening_phrases) <= set(_summarization().phrases)

    def test_no_other_class_declares_a_short_prompt(self) -> None:
        """Scope: only the class the shadow run measured is widened."""
        authority = load_task_class_authority()
        widened = sorted(
            name
            for name, entry in authority.task_classes.items()
            if entry.selection.short_prompt is not None
        )
        assert widened == ["summarization"]


class TestAnUnusableBlockIsRefused:
    @pytest.mark.parametrize(
        ("selection", "why"),
        [
            (
                {
                    "priority": 80,
                    "min_words": 120,
                    "phrases": ["summary"],
                    "short_prompt": {"min_words": 6, "opening_phrases": ["summarize"]},
                },
                "an opening phrase the class does not claim at full length",
            ),
            (
                {
                    "priority": 80,
                    "phrases": ["summarize"],
                    "short_prompt": {"min_words": 6, "opening_phrases": ["summarize"]},
                },
                "no class floor for the block to admit prompts below",
            ),
            (
                {
                    "priority": 80,
                    "min_words": 120,
                    "phrases": ["summarize"],
                    "short_prompt": {
                        "min_words": 120,
                        "opening_phrases": ["summarize"],
                    },
                },
                "a block floor that is not below the class floor",
            ),
            (
                {
                    "priority": 80,
                    "min_words": 120,
                    "phrases": ["summarize"],
                    "short_prompt": {"min_words": 6, "opening_phrases": []},
                },
                "no opening phrases",
            ),
            (
                {
                    "priority": 80,
                    "min_words": 120,
                    "phrases": ["summarize"],
                    "short_prompt": {"min_words": 6, "opening_phrases": ["Summarize"]},
                },
                "an opening phrase that is not lowercase",
            ),
        ],
    )
    def test_the_block_is_refused(self, selection: dict[str, object], why: str) -> None:
        with pytest.raises(ValidationError):
            ModelTaskClassSelection.model_validate(selection)

    def test_a_usable_block_loads(self) -> None:
        """Positive control: the refusals above are about the defects they name."""
        selection = ModelTaskClassSelection.model_validate(
            {
                "priority": 80,
                "min_words": 120,
                "phrases": ["summarize"],
                "short_prompt": {"min_words": 6, "opening_phrases": ["summarize"]},
            }
        )
        assert selection.short_prompt is not None
