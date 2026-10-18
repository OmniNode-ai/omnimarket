# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The extraction fold is identical under every arrival order and duplicates (OMN-21139, TE.2)."""

from __future__ import annotations

import builtins
import itertools
import json
from pathlib import Path

import pytest
from omnibase_core.enums.enum_context_factor import EnumContextFactor

from omnimarket.models.transcript_extraction import (
    EnumCapturedPromptOrigin,
    EnumExtractionRejectionReason,
    ModelCapturedPrompt,
    ModelExtractionFoldRequest,
    ModelExtractionFoldResult,
    ModelExtractionVerifyRequest,
    ModelExtractionVerifyResult,
)
from omnimarket.nodes.node_extraction_fold_compute.handlers.handler_extraction_fold import (
    HandlerExtractionFold,
)
from omnimarket.nodes.node_extraction_verify_compute.handlers.handler_extraction_verify import (
    HandlerExtractionVerify,
)

pytestmark = pytest.mark.unit

FIRST = "Ship the docs today. Do not deploy without approval."
SECOND = "Keep the queue clear. Hold every merge until noon."
PROMPTS = (
    ModelCapturedPrompt(
        event_id="e-1",
        prompt_id="p-1",
        text=FIRST,
        origin=EnumCapturedPromptOrigin.OPERATOR_TYPED,
    ),
    ModelCapturedPrompt(
        event_id="e-2",
        prompt_id="p-2",
        text=SECOND,
        origin=EnumCapturedPromptOrigin.OPERATOR_TYPED,
    ),
)


def proposal(text: str, word: str, prompt_id: str, event_id: str) -> str:
    start = text.index(word)
    return json.dumps(
        {
            "kind": "ruling",
            "quote": word,
            "event_id": event_id,
            "prompt_id": prompt_id,
            "covers_start": start,
            "covers_end": start + len(word),
        }
    )


def segment(segment_id: str, *proposals: str) -> ModelExtractionVerifyResult:
    return HandlerExtractionVerify().handle(
        ModelExtractionVerifyRequest(
            segment_id=segment_id, prompts=PROMPTS, proposals=proposals
        )
    )


SEGMENTS = (
    segment("seg-a", proposal(FIRST, "deploy", "p-1", "e-1"), "garbage"),
    # overlapping window: proposes the same ruling through another word, and a new one
    segment(
        "seg-b",
        proposal(FIRST, "approval", "p-1", "e-1"),
        proposal(SECOND, "merge", "p-2", "e-2"),
    ),
    segment(
        "seg-c", proposal(SECOND, "queue", "p-2", "e-2"), json.dumps({"kind": "x"})
    ),
)


def fold(*results: ModelExtractionVerifyResult) -> ModelExtractionFoldResult:
    return HandlerExtractionFold().handle(
        ModelExtractionFoldRequest(segment_results=results)
    )


def test_the_fold_is_identical_under_every_arrival_order() -> None:
    folds = {
        fold(*order).model_dump_json() for order in itertools.permutations(SEGMENTS)
    }
    assert len(folds) == 1


def test_the_fold_is_identical_under_duplicate_segments() -> None:
    base = fold(*SEGMENTS)
    doubled = SEGMENTS + SEGMENTS[:2]
    for order in itertools.permutations(doubled):
        assert fold(*order) == base


def test_the_same_candidate_from_overlapping_segments_is_kept_once() -> None:
    result = fold(*SEGMENTS)
    quotes = [c.quote for c in result.candidates]
    assert quotes == [
        "Do not deploy without approval.",
        "Keep the queue clear.",
        "Hold every merge until noon.",
    ]
    assert len({c.candidate_digest for c in result.candidates}) == 3


def test_rejection_counts_sum_over_distinct_segments() -> None:
    result = fold(*SEGMENTS, SEGMENTS[0])
    assert result.rejection_counts[EnumExtractionRejectionReason.PARSE_FAILURE] == 2
    assert sum(result.rejection_counts.values()) == 2
    assert result.segment_ids == ("seg-a", "seg-b", "seg-c")


def test_two_different_results_under_one_segment_id_fold_the_same_either_way() -> None:
    other = segment("seg-a", proposal(SECOND, "merge", "p-2", "e-2"))
    assert fold(SEGMENTS[0], other) == fold(other, SEGMENTS[0])


def test_folded_candidates_stay_transcript_extracts_never_rulings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the fold performed I/O")

    monkeypatch.setattr(builtins, "open", refuse)
    monkeypatch.setattr(Path, "open", refuse)
    monkeypatch.setattr(Path, "read_text", refuse)
    result = fold(*SEGMENTS)
    assert result.candidates
    for candidate in result.candidates:
        assert candidate.factor is EnumContextFactor.TRANSCRIPT_EXTRACTS
        assert candidate.verbatim_verified is True


def test_an_empty_fold_has_no_candidates() -> None:
    result = fold()
    assert result.candidates == ()
    assert set(result.rejection_counts.values()) == {0}
