# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The frozen baseline retrieval, the frozen question set and its scorer, each with a control."""

from __future__ import annotations

import hashlib
import json

import pytest

from omnimarket.nodes.node_memory_pilot_effect.handlers.lexical_retrieval import (
    CONTEXT_CHARS,
    TOP_FILES,
    question_terms,
    retrieve,
)
from omnimarket.nodes.node_memory_pilot_effect.handlers.pilot_scoring import (
    HARD_CATEGORIES,
    load_questions,
    score_answer,
)
from omnimarket.nodes.node_memory_pilot_effect.models.model_memory_pilot import (
    ModelMemoryPilotDocument,
)

from .conftest import PILOT_DIR

pytestmark = pytest.mark.unit


def test_terms_drop_short_words_and_stop_words_and_repeat_once() -> None:
    assert question_terms(
        "What is the maximum number of retries the Gamma scheduler allows per job? Gamma!"
    ) == (
        "maximum",
        "number",
        "retries",
        "gamma",
        "scheduler",
        "allows",
        "per",
        "job",
    )


def test_retrieval_ranks_by_distinct_terms_then_hits_and_caps_files_and_chars() -> None:
    docs = tuple(
        ModelMemoryPilotDocument(
            doc_id=f"f{i}.md",
            text=("alpha beta gamma. " * (i + 1)) + "\n\n" + "x" * 5000,
        )
        for i in range(8)
    )
    docs += (
        ModelMemoryPilotDocument(
            doc_id="best.md", text="alpha beta gamma delta together"
        ),
    )
    refs = retrieve(docs, "alpha beta gamma delta")
    assert refs[0].source == "best.md", "most distinct terms first"
    assert len({r.source for r in refs}) <= TOP_FILES
    assert sum(len(r.excerpt) for r in refs) <= CONTEXT_CHARS
    assert retrieve(docs, "zzzz qqqq") == (), "no matching term retrieves nothing"


def test_retrieval_is_deterministic() -> None:
    docs = tuple(
        ModelMemoryPilotDocument(doc_id=f"f{i}.md", text="alpha beta")
        for i in (3, 1, 2)
    )
    assert retrieve(docs, "alpha beta") == retrieve(tuple(reversed(docs)), "alpha beta")


def test_frozen_listing_matches_every_file_it_names() -> None:
    lines = (PILOT_DIR / "FROZEN.sha256").read_text().splitlines()
    assert len(lines) == 9
    for line in lines:
        digest, _, name = line.partition("  ")
        assert hashlib.sha256((PILOT_DIR / name).read_bytes()).hexdigest() == digest


def test_question_set_shape_and_scorer_controls() -> None:
    qs = load_questions(PILOT_DIR)
    ids = [q.id for q in qs.questions]
    assert len(ids) == len(set(ids)) == 36
    cats = {
        c: sum(q.category == c for q in qs.questions)
        for c in {q.category for q in qs.questions}
    }
    assert cats == {
        "lexical": 9,
        "paraphrase": 8,
        "multihop": 8,
        "aggregate": 3,
        "planted": 6,
        "abstain": 2,
    }
    assert sum(q.category in HARD_CATEGORIES for q in qs.questions) == 11
    for q in qs.questions:
        if q.category != "abstain":
            assert score_answer(q, q.gold, qs.abstain_regex), (
                f"gold fails its own scorer: {q.id}"
            )
            assert not score_answer(q, "", qs.abstain_regex), (
                f"empty answer scores: {q.id}"
            )
            assert not score_answer(q, "NOT IN CONTEXT", qs.abstain_regex), (
                f"abstention scores: {q.id}"
            )
        else:
            assert score_answer(q, "NOT IN CONTEXT", qs.abstain_regex)
            assert not score_answer(
                q, "The answer is 42. " + "x" * 700, qs.abstain_regex
            )


def test_manifest_counts_match_the_bar() -> None:
    manifest = json.loads((PILOT_DIR / "corpus_manifest.json").read_text())
    assert manifest["file_count"] == len(manifest["files"]) == 71
    assert manifest["source_sha"] == "a6a1d1125a47d483b8a76eb3ee4f9be3749eef1f"
    bar = json.loads((PILOT_DIR / "bar.json").read_text())
    assert bar["product"]["version_pin"] == "1.6.6"
    assert bar["answering"]["trials"] == 3
