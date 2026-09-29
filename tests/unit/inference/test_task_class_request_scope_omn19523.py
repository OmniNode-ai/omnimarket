# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Task-class selection reads the request, not the material it carries (OMN-19523).

The delegation capability matrix of 2026-09-25 recorded eight trial failures
tagged ``wrong_classification``. In each, a class phrase occurred somewhere the
caller did not put a request: a ``pytest`` comment inside a fenced YAML merge
conflict (run 6bfe152a), a quoted linter line reading ``needs review`` (run
20118c8a), quoted sample prompts opening ``Summarize in one sentence`` (runs
40e9de70, d83aae1f), a negated ``no summary of the change`` (run a8219faf),
and nouns in a specification (``an immutable digest``, ``module docstring``;
runs ea0b2d12, 15fffef6).

WHAT THIS FILE PINS, by acceptance criterion:

* AC1 a phrase only inside a fenced block resolves as if the block were absent;
* AC2 a phrase inside a quoted string does not claim;
* AC3 a negated phrase does not claim;
* AC4 the shape gates count the request's words, not the payload's;
* AC5 the recorded prompts, committed verbatim, each resolve to the class the
  work needed (``tests/fixtures/delegation/omn19523/misrouted_prompts.yaml``);
* AC6 is the unchanged ``--task-type`` path here plus every existing selection
  test in ``tests/unit/cli``.

The routing rows run against the live Market authority and its declared
vocabulary, with no mirrored contract or pinned class-name list.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.inference.request_instruction import (
    instruction_text,
    instruction_word_count,
    is_negated,
    opening_sentence,
)
from omnimarket.inference.task_class_authority import (
    EnumTaskTypeResolution,
    ModelTaskClassAuthority,
    load_task_class_authority,
)

pytestmark = pytest.mark.unit

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "delegation"
_CORPUS = _FIXTURES / "omn19523" / "misrouted_prompts.yaml"


@pytest.fixture(name="production")
def _production() -> ModelTaskClassAuthority:
    return load_task_class_authority()


def _resolve(prompt: str, classes: ModelTaskClassAuthority) -> str:
    return classes.resolve_task_type(prompt, explicit=None).task_type


def _corpus() -> list[dict[str, object]]:
    raw = yaml.safe_load(_CORPUS.read_text(encoding="utf-8"))
    rows: list[dict[str, object]] = raw["prompts"]
    return rows


def _prompt(row: dict[str, object]) -> str:
    return (_CORPUS.parent / str(row["file"])).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# AC1: fenced code is material.
# ---------------------------------------------------------------------------


_REQUEST = (
    "Resolve this git merge conflict in a hooks file and output only the "
    "resolved text in one block."
)

_FENCED_PHRASES = (
    "      # runs inside the required pytest job",
    "Summarize in one sentence what the hook does.",
    "Complex union pattern needs review",
    "Traceback (most recent call last):",
    "Please write a test for the parser module.",
)


class TestFencedBlocksAreMaterial:
    @pytest.mark.parametrize("payload", _FENCED_PHRASES)
    @pytest.mark.parametrize("fence", ["```", "~~~", "````"])
    def test_a_phrase_only_inside_a_fence_resolves_as_if_absent(
        self,
        production: ModelTaskClassAuthority,
        payload: str,
        fence: str,
    ) -> None:
        with_block = f"{_REQUEST}\n\n{fence}yaml\n{payload}\n{fence}\n"
        without_block = f"{_REQUEST}\n"
        assert _resolve(with_block, production) == _resolve(without_block, production)

    def test_an_unclosed_fence_runs_to_the_end(self) -> None:
        """CommonMark: an unclosed fence is code to the end of the document."""
        text = instruction_text(f"{_REQUEST}\n```\nwrite a test for the parser\n")
        assert "write a test" not in text
        assert "Resolve this git merge conflict" in text

    def test_a_shorter_fence_does_not_close_a_longer_one(self) -> None:
        prompt = f"{_REQUEST}\n````\n```\npytest in the payload\n````\nafter"
        text = instruction_text(prompt)
        assert "pytest" not in text
        assert "after" in text

    def test_inline_code_is_material(self, production: ModelTaskClassAuthority) -> None:
        prompt = "Rename nothing; explain the flag `--summary` in two sentences."
        assert "summary" not in instruction_text(prompt)
        assert production.resolve_task_type(prompt, explicit=None)

    def test_a_path_is_a_name_not_words(self) -> None:
        """Run 87a0138d: ``tests/`` in a pasted command qualified ``pytest``."""
        text = instruction_text(
            "Run uv run pytest tests/nodes/node_x -q in OmniNode-ai/omnimarket"
        )
        assert "tests/" not in text
        assert "omnimarket" not in text
        assert "pytest" in text

    def test_the_recorded_merge_conflict_no_longer_reads_pytest(self) -> None:
        """Run 6bfe152a, verbatim: its only ``pytest`` is inside the fence."""
        row = next(row for row in _corpus() if row["trial"] == "t2a")
        prompt = _prompt(row)
        assert "pytest" in prompt
        assert "pytest" not in instruction_text(prompt).lower()


# ---------------------------------------------------------------------------
# AC2: quoted strings are material.
# ---------------------------------------------------------------------------


class TestQuotedStringsAreMaterial:
    @pytest.mark.parametrize(
        "prompt",
        [
            'Fix the check the linter flags as "needs review" here.',
            "Fix the check the linter flags as \u201cneeds review\u201d here.",
            "Fix the check the linter flags as 'needs review' here.",
            "Fix the check the linter flags as \u2018needs review\u2019 here.",
        ],
    )
    def test_a_quoted_phrase_does_not_claim(
        self, production: ModelTaskClassAuthority, prompt: str
    ) -> None:
        assert _resolve(prompt, production) != "review"

    def test_an_apostrophe_never_opens_a_quote(self) -> None:
        """``don't`` and ``lanes'`` must not swallow the request between them."""
        text = instruction_text("Don't guess; the lanes' rows say review it.")
        assert "review it" in text

    def test_a_quote_never_spans_lines(self) -> None:
        text = instruction_text('He said "stop\nand review the diff please.')
        assert "review the diff" in text

    @pytest.mark.parametrize("trial", ["c2-t1", "c2-t3"])
    def test_the_recorded_quoted_sample_prompts_no_longer_claim(
        self, production: ModelTaskClassAuthority, trial: str
    ) -> None:
        """Runs 40e9de70 and d83aae1f, verbatim: each sample opened "Summarize".

        The ticket's AC2 names run ea0b2d12 for this case; that run's phrase was
        the unquoted noun ``digest`` and is covered by AC5 instead. The
        quoted-sample prompts are these two.
        """
        row = next(row for row in _corpus() if row["trial"] == trial)
        prompt = _prompt(row)
        assert row["was"] == "summarization"
        assert _resolve(prompt, production) != "summarization"


# ---------------------------------------------------------------------------
# AC3: a negated phrase says what is NOT wanted.
# ---------------------------------------------------------------------------


class TestNegatedPhrasesDoNotClaim:
    @pytest.mark.parametrize(
        ("prompt", "refused"),
        [
            ("This is not a review. Rename the helper in the parser.", "review"),
            (
                "Do not summarize the notes; list every open question in the "
                "notes as a numbered list, with the owner named for each one.",
                "summarization",
            ),
            ("No summary of the change, just the three risks.", "summarization"),
            ("Without a code review, tell me what this flag does.", "code_review"),
        ],
    )
    def test_the_negated_class_is_not_chosen(
        self,
        production: ModelTaskClassAuthority,
        prompt: str,
        refused: str,
    ) -> None:
        assert _resolve(prompt, production) != refused

    def test_a_clause_break_ends_the_negation(self) -> None:
        text = "if it is not ready, review it"
        assert not is_negated(text, text.index("review"))

    def test_the_window_is_three_words(self) -> None:
        near = "not a code review"
        far = "not the slow and careful review"
        assert is_negated(near, near.index("code review"))
        assert not is_negated(far, far.index("review"))

    def test_a_later_plain_occurrence_still_claims(
        self, production: ModelTaskClassAuthority
    ) -> None:
        prompt = "Not a code review of style. Do a code review of the logic."
        assert _resolve(prompt, production) == "code_review"

    def test_a_negated_veto_does_not_veto(
        self, production: ModelTaskClassAuthority
    ) -> None:
        """ "do not write a PR description" names no prose output."""
        prompt = "Implement the retry helper; do not write a pr description."
        assert _resolve(prompt, production) == "code_generation"

    def test_a_negated_qualified_phrase_leaves_a_later_plain_occurrence_eligible(
        self, production: ModelTaskClassAuthority
    ) -> None:
        selection = production.task_classes["code_generation"].selection
        assert selection.matching_phrase("do not write a parser") is None
        prompt = "Do not write a parser. Write a function to read the manifest."
        assert _resolve(prompt, production) == "code_generation"

    def test_a_later_plain_veto_still_vetoes_the_opening_sentence(
        self, production: ModelTaskClassAuthority
    ) -> None:
        prompt = (
            "Implement the retry helper. Do not write a pr description yet. "
            "Return a pr description when finished."
        )
        resolution = production.resolve_task_type(prompt, explicit=None)
        assert production.selection_fallback is not None
        assert resolution.task_type == production.selection_fallback.task_class
        assert "; vetoed: 'code_generation' matched" in resolution.reason
        assert "'pr description'" in resolution.reason

    def test_the_recorded_negation_no_longer_claims(
        self, production: ModelTaskClassAuthority
    ) -> None:
        """Run a8219faf, verbatim: "No praise, no summary of the change".

        The ticket's AC3 names run 20118c8a for this case; that run's phrase sat
        inside a quoted linter line and is covered by AC2's rule and AC5.
        """
        row = next(row for row in _corpus() if row["trial"] == "t3a")
        assert _resolve(_prompt(row), production) != "summarization"


# ---------------------------------------------------------------------------
# AC4: the shape gates count the request's words.
# ---------------------------------------------------------------------------


_THIRTY_WORD_REQUEST = (
    "Implement a helper that reads the lane manifest and returns the declared "
    "services in order, keeping comments out, and reply with only the Python "
    "function in one fenced block please."
)


class TestShapeCountsTheRequest:
    def test_the_request_is_thirty_words(self) -> None:
        assert len(_THIRTY_WORD_REQUEST.split()) == 30

    def test_a_500_word_payload_is_not_counted(
        self, production: ModelTaskClassAuthority
    ) -> None:
        payload = "\n".join(["word " * 10] * 50)
        prompt = f"{_THIRTY_WORD_REQUEST}\n\n```text\n{payload}\n```\n"
        assert len(prompt.split()) > 500
        assert instruction_word_count(prompt) == 30
        resolution = production.resolve_task_type(prompt, explicit=None)
        assert "30-word request" in resolution.reason

    def test_the_ceiling_admits_a_short_request_with_a_long_payload(
        self, production: ModelTaskClassAuthority
    ) -> None:
        """code_generation's 600-word ceiling no longer counts pasted code."""
        payload = "\n".join(["x = 1  # line"] * 400)
        prompt = f"{_THIRTY_WORD_REQUEST}\n\n```python\n{payload}\n```\n"
        assert len(prompt.split()) > 600
        assert _resolve(prompt, production) == "code_generation"

    def test_the_floor_is_not_met_by_payload(
        self, production: ModelTaskClassAuthority
    ) -> None:
        """summarization's 120-word floor is not met by a pasted block."""
        payload = "\n".join(["the deploy went out and the lane stayed green"] * 60)
        prompt = f"Summarize this log.\n\n```text\n{payload}\n```\n"
        assert len(prompt.split()) > 120
        assert _resolve(prompt, production) != "summarization"


# ---------------------------------------------------------------------------
# The opening sentence names the work.
# ---------------------------------------------------------------------------


class TestTheOpeningSentence:
    def test_markers_and_courtesy_are_removed(self) -> None:
        assert opening_sentence("## 1. Please write a parser. Then stop.") == (
            "write a parser"
        )

    def test_an_empty_request_opens_with_nothing(self) -> None:
        assert opening_sentence("\n\n   \n") == ""

    def test_a_noun_later_in_the_spec_no_longer_outranks_the_verb(
        self, production: ModelTaskClassAuthority
    ) -> None:
        prompt = (
            "Write three Pydantic v2 model modules. The first holds an immutable "
            "digest and a tag, the second a one-line docstring per member."
        )
        resolution = production.resolve_task_type(prompt, explicit=None)
        assert resolution.task_type == "code_generation"
        assert "opening sentence" in resolution.reason

    def test_a_request_that_opens_with_context_is_read_whole(
        self, production: ModelTaskClassAuthority
    ) -> None:
        """A descriptive opening ("The unit tests passed") selects nothing."""
        facts = "The deploy went out at noon and the lane stayed green. " * 12
        prompt = f"The unit tests passed yesterday. {facts}Summarize the day."
        assert _resolve(prompt, production) == "summarization"

    def test_an_opening_that_selects_nothing_falls_back_to_the_whole(
        self, production: ModelTaskClassAuthority
    ) -> None:
        prompt = "Write the three answers below as bullets. Review the diff."
        assert _resolve(prompt, production) == "code_review"


# ---------------------------------------------------------------------------
# AC5: the recorded prompts, verbatim.
# ---------------------------------------------------------------------------


_ROUTED = [row for row in _corpus() if row["needed_class"] is not None]
_RESIDUAL = [row for row in _corpus() if row["needed_class"] is None]


class TestTheRecordedPromptsReplay:
    def test_the_corpus_holds_the_eight_tagged_failures(self) -> None:
        """The matrix report's wrong_classification row names these eight."""
        tagged = {"t1d", "t2a", "t3a", "c2-t1", "c2-t3", "c3-t1", "c1-t4", "t8"}
        assert tagged <= {str(row["trial"]) for row in _corpus()}

    @pytest.mark.parametrize("row", _ROUTED, ids=[str(r["trial"]) for r in _ROUTED])
    def test_each_prompt_resolves_to_the_class_the_work_needed(
        self,
        production: ModelTaskClassAuthority,
        row: dict[str, object],
    ) -> None:
        resolved = _resolve(_prompt(row), production)
        assert resolved == row["needed_class"], (
            f"{row['trial']} (run {row['run']}) resolved {resolved!r}; the work "
            f"needed {row['needed_class']!r}. {row['evidence']}"
        )
        assert resolved != row["was"]

    @pytest.mark.parametrize("row", _RESIDUAL, ids=[str(r["trial"]) for r in _RESIDUAL])
    def test_each_residual_still_carries_its_phrase_in_the_request(
        self, row: dict[str, object]
    ) -> None:
        """A residual is honest only while its phrase really is request text."""
        assert str(row["was_phrase"]) in instruction_text(_prompt(row)).lower()
        assert row["residual"]


# ---------------------------------------------------------------------------
# AC6: the explicit path is untouched.
# ---------------------------------------------------------------------------


class TestTheExplicitPathIsUnchanged:
    def test_an_explicit_class_wins_over_any_payload(
        self, production: ModelTaskClassAuthority
    ) -> None:
        prompt = "```\nsummarize\n```\nnot a review"
        resolution = production.resolve_task_type(prompt, explicit="test")
        assert resolution.task_type == "test"
        assert resolution.resolution is EnumTaskTypeResolution.EXPLICIT
