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

The routing tests replay the upstream shadow corpus against the live Market
authority, which now owns the evaluator as well as the contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.inference.task_class_authority import (
    EnumTaskTypeResolution,
    ModelTaskClassAuthority,
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


_CORPUS = (
    Path(__file__).resolve().parents[2]
    / "fixtures/delegation/omn19140/shadow_corpus.yaml"
)

#: The captured 9-word prompt the OMN-19136 report used as its positive control.
_CAPTURED_SHORT = "Summarize in one sentence: the broker accepted this request."

_CORPUS_ROWS: list[dict[str, object]] = yaml.safe_load(
    _CORPUS.read_text(encoding="utf-8")
)["rows"]


@pytest.fixture(name="production")
def _production() -> ModelTaskClassAuthority:
    return load_task_class_authority()


def _resolve(prompt: str, classes: ModelTaskClassAuthority) -> str:
    return classes.resolve_task_type(prompt, explicit=None).task_type


class TestTheShadowCorpus:
    """AC5: the corpus is the shadow run's own prompts, not a hand-written list."""

    def test_the_corpus_is_the_one_the_report_scored(self) -> None:
        """Positive control on the fixture: 21 rows, 15 of them the floor rows."""
        assert len(_CORPUS_ROWS) == 21
        floor_rows = [
            row
            for row in _CORPUS_ROWS
            if row["incumbent_before"] != "summarization"
            and row["shadow_label"] == "summarization"
        ]
        assert len(floor_rows) == 15
        for row in _CORPUS_ROWS:
            assert len(str(row["prompt"]).split()) == row["words"], row["row"]

    def test_every_floor_row_was_below_the_floor(self) -> None:
        """The 15 are the defect only if length alone kept them out."""
        for row in _CORPUS_ROWS:
            if row["shadow_label"] == "summarization" and (
                row["incumbent_before"] != "summarization"
            ):
                assert int(str(row["words"])) < 120, row["row"]

    @pytest.mark.parametrize(
        "row", _CORPUS_ROWS, ids=[f"row-{row['row']}" for row in _CORPUS_ROWS]
    )
    def test_each_row_resolves_as_required(
        self, production: ModelTaskClassAuthority, row: dict[str, object]
    ) -> None:
        resolution = production.resolve_task_type(str(row["prompt"]), explicit=None)
        assert resolution.task_type == row["expected_after"], (
            f"row {row['row']} ({row['why']}): {resolution.reason}"
        )
        if row["expected_after"] == "summarization":
            assert resolution.resolution is EnumTaskTypeResolution.CONTRACT


class TestTheShortRequest:
    """AC1 and AC2: one variable, the word count, and the same answer either side."""

    def test_the_captured_short_request_resolves_to_summarization(
        self, production: ModelTaskClassAuthority
    ) -> None:
        resolution = production.resolve_task_type(_CAPTURED_SHORT, explicit=None)
        assert resolution.task_type == "summarization", resolution.reason
        assert resolution.resolution is EnumTaskTypeResolution.CONTRACT
        assert "at the start of a 9-word prompt" in resolution.reason

    def test_the_padded_control_still_resolves_to_summarization(
        self, production: ModelTaskClassAuthority
    ) -> None:
        padded = _CAPTURED_SHORT + " The broker accepted this request." * 25
        assert len(padded.split()) >= 120
        resolution = production.resolve_task_type(padded, explicit=None)
        assert resolution.task_type == "summarization", resolution.reason
        assert resolution.resolution is EnumTaskTypeResolution.CONTRACT
        assert "at the start" not in resolution.reason

    def test_the_verb_in_second_position_still_counts_when_the_prompt_is_long(
        self, production: ModelTaskClassAuthority
    ) -> None:
        """Above the floor the class claims on presence, exactly as before."""
        long_prompt = "Please summarize this. " + "The broker accepted it. " * 30
        assert _resolve(long_prompt, production) == "summarization"


class TestTheShortRequestIsCountedOnTheRequestOnly:
    """OMN-19140 composed with OMN-19523: the floor counts the request's words.

    Pasted material (a fenced block) is not part of the request, so a short
    request carrying long material is still a short request, admitted by its
    opening verb, and the reason line says so.
    """

    def test_fenced_material_does_not_lift_a_short_request_over_the_floor(
        self, production: ModelTaskClassAuthority
    ) -> None:
        material = "\n".join(f"line {index} of the pasted log" for index in range(60))
        prompt = f"{_CAPTURED_SHORT}\n\n```\n{material}\n```"
        assert len(prompt.split()) >= 120
        resolution = production.resolve_task_type(prompt, explicit=None)
        assert resolution.task_type == "summarization", resolution.reason
        assert resolution.resolution is EnumTaskTypeResolution.CONTRACT
        assert "at the start of a 9-word prompt" in resolution.reason


class TestTheThinPromptStaysOut:
    """AC4: the negative controls. A prompt with nothing to summarise."""

    @pytest.mark.parametrize(
        "prompt",
        [
            "Summarize this.",
            "Summarize the standup.",
            "Summarize in one sentence:",
            "Summarise what happened today.",
            "summarize the incident for me",
        ],
    )
    def test_a_thin_request_does_not_reach_summarization(
        self, production: ModelTaskClassAuthority, prompt: str
    ) -> None:
        assert len(prompt.split()) < 6
        resolution = production.resolve_task_type(prompt, explicit=None)
        assert resolution.task_type != "summarization", resolution.reason

    def test_the_thin_floor_is_the_only_difference(
        self, production: ModelTaskClassAuthority
    ) -> None:
        """Control: one more word of subject and the same request is admitted."""
        assert _resolve("summarize the incident for the operator", production) == (
            "summarization"
        )


class TestTheFloorStillDoesItsJob:
    """AC3: what the 120-word floor protected is still protected."""

    @pytest.mark.parametrize(
        ("prompt", "why"),
        [
            (
                "Review this one-line summary for accuracy and reply ACCURATE or "
                "INACCURATE with one sentence of reason: 'A registry workspace now "
                "resolves its delegation transport from its own configuration, so "
                "a fresh workspace dispatches to the shared lane without any "
                "per-user setup.'",
                "the noun names the object under review, not the deliverable",
            ),
            (
                "Add a summary field to the run model.",
                "the noun in a short code-shaped request",
            ),
            (
                "Implement a helper that returns the sha256 digest of a file.",
                "the noun 'digest' in a short code request",
            ),
            (
                "Write a test for the digest helper.",
                "the noun 'digest' in a short test request",
            ),
            (
                "Implement a function that can summarize log lines.",
                "the verb mid-sentence in a short code request",
            ),
            (
                "Tell me what happened with the build yesterday.",
                "'what happened' in a short question",
            ),
        ],
    )
    def test_an_incidental_phrase_does_not_claim_a_short_prompt(
        self,
        production: ModelTaskClassAuthority,
        prompt: str,
        why: str,
    ) -> None:
        assert len(prompt.split()) < 120
        resolution = production.resolve_task_type(prompt, explicit=None)
        assert resolution.task_type != "summarization", f"{why}: {resolution.reason}"

    def test_the_code_requests_route_to_their_own_classes(
        self, production: ModelTaskClassAuthority
    ) -> None:
        """Positive control: the rows above are refused for the right reason."""
        assert (
            _resolve(
                "Implement a helper that returns the sha256 digest of a file.",
                production,
            )
            == "code_generation"
        )
        assert _resolve("Write a test for the digest helper.", production) == "test"
