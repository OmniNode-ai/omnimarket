# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The transcript chunker is lossless, overlap-bounded and never cuts a fence (OMN-21139, TE.2)."""

from __future__ import annotations

import random

import pytest
from pydantic import ValidationError

from omnimarket.models.transcript_extraction import (
    ModelTranscriptChunkRequest,
    ModelTranscriptChunkResult,
)
from omnimarket.nodes.node_transcript_chunk_compute.handlers.handler_transcript_chunk import (
    HandlerTranscriptChunk,
)

pytestmark = pytest.mark.unit

FENCE = "```python\ndef f():\n    return 1\n\n\nprint(f())\n```\n"
TABLE = "| a | b |\n| - | - |\n| 1 | 2 |\n| 3 | 4 |\n"
RULING = "Do not deploy without approval."
PROSE = (
    "The operator asked for the queue to drain first. Then the lane ran.\n\n"
    "A second paragraph follows here, with more words in it.\n"
)


def chunk(text: str, max_chars: int, overlap: int = 0) -> ModelTranscriptChunkResult:
    return HandlerTranscriptChunk().handle(
        ModelTranscriptChunkRequest(
            text=text, max_chars=max_chars, overlap_chars=overlap
        )
    )


def generated_texts() -> list[str]:
    rng = random.Random(21139)
    pieces = [
        "word ",
        "two words. ",
        "\n",
        "\n\n",
        "x" * 37,
        FENCE,
        TABLE,
        RULING + " ",
        "é ünï 文字 ",
        "  \t",
        "?! ",
    ]
    texts = ["", "a", " ", "\n", "x" * 500, FENCE * 3, TABLE + FENCE + PROSE]
    for _ in range(150):
        texts.append("".join(rng.choice(pieces) for _ in range(rng.randint(1, 60))))
    return texts


def assert_partition(
    text: str, result: ModelTranscriptChunkResult, overlap: int
) -> None:
    chunks = result.chunks
    assert "".join(text[c.core_start : c.core_end] for c in chunks) == text
    expected_start = 0
    for index, c in enumerate(chunks):
        assert c.index == index
        assert c.core_start == expected_start, "a gap or an overlap between cores"
        assert c.core_end > c.core_start, "an empty chunk"
        assert c.end == c.core_end
        assert c.start == max(0, c.core_start - overlap), (
            "overlap beyond the declared one"
        )
        assert c.text == text[c.start : c.end]
        expected_start = c.core_end
    assert expected_start == len(text)


# Partition and termination (AC8)


@pytest.mark.parametrize(
    ("max_chars", "overlap"), [(1, 0), (7, 3), (40, 0), (64, 20), (300, 50)]
)
def test_cores_partition_the_text_and_join_to_the_original(
    max_chars: int, overlap: int
) -> None:
    for text in generated_texts():
        assert_partition(text, chunk(text, max_chars, overlap), overlap)


def test_the_fixtures_join_to_the_original() -> None:
    for text in (PROSE * 5, FENCE + PROSE + TABLE, RULING):
        result = chunk(text, 50, 10)
        assert_partition(text, result, 10)


def test_it_terminates_when_the_final_span_ends_at_the_end_of_the_text() -> None:
    text = "abc def ghi"
    result = chunk(text, 5)
    assert result.chunks[-1].core_end == len(text)
    assert len(result.chunks) <= len(text)


def test_an_empty_text_gives_no_chunks_and_a_short_text_one() -> None:
    assert chunk("", 10).chunks == ()
    short = chunk(RULING, 1000)
    assert short.needs_chunking is False
    assert [c.text for c in short.chunks] == [RULING]
    assert chunk(PROSE, 10).needs_chunking is True


def test_an_overlap_not_below_the_budget_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelTranscriptChunkRequest(text="abc", max_chars=4, overlap_chars=4)


# Fences, tables, ids, no drops (AC9)


@pytest.mark.parametrize("block", [FENCE, TABLE])
def test_a_fence_or_table_is_never_cut(block: str) -> None:
    text = PROSE + block + PROSE
    begin = len(PROSE)
    finish = begin + len(block)
    for max_chars in (5, 12, 30, 60, 200):
        result = chunk(text, max_chars, 2)
        assert_partition(text, result, 2)
        for c in result.chunks:
            assert not begin < c.core_end < finish, (max_chars, c.core_end)


def test_a_block_larger_than_the_budget_is_kept_whole_and_flagged_oversize() -> None:
    text = "intro\n" + FENCE
    result = chunk(text, 10)
    fenced = [c for c in result.chunks if FENCE in c.text]
    assert len(fenced) == 1
    assert fenced[0].oversize is True


def test_an_unclosed_fence_runs_to_the_end_and_is_not_cut() -> None:
    text = "intro\n```\nline one\nline two\nline three\n"
    result = chunk(text, 8)
    assert_partition(text, result, 0)
    assert result.chunks[-1].core_start <= text.index("```")


def test_chunk_ids_are_content_hashes_identical_on_rerun() -> None:
    text = PROSE * 4 + FENCE
    first = chunk(text, 40, 8)
    second = chunk(text, 40, 8)
    assert [c.chunk_id for c in first.chunks] == [c.chunk_id for c in second.chunks]
    assert all(c.chunk_id.startswith("chunk_") for c in first.chunks)
    assert len({c.chunk_id for c in first.chunks}) == len(first.chunks)
    changed = chunk(text.replace("queue", "queues"), 40, 8)
    assert first.chunks[0].chunk_id != changed.chunks[0].chunk_id


def test_no_chunk_is_dropped_for_size_or_word_count() -> None:
    text = "a\n\nb\n\n" + "c" * 30 + "\n\nd"
    result = chunk(text, 6)
    assert_partition(text, result, 0)
    assert (
        any(c.text.strip() == "a" for c in result.chunks)
        or "a" in result.chunks[0].text
    )


def test_cuts_prefer_paragraph_and_line_boundaries() -> None:
    text = "one two three four.\n\nfive six seven eight nine ten eleven"
    result = chunk(text, 30)
    assert result.chunks[0].core_end == text.index("five")


# A ruling across a segment boundary (AC5)


def test_a_ruling_across_a_core_boundary_is_whole_in_one_window() -> None:
    text = "x " * 40 + RULING + " y" * 40
    overlap = len(RULING)
    result = chunk(text, 50, overlap)
    begin = text.index(RULING)
    finish = begin + len(RULING)
    assert any(begin < c.core_end < finish for c in result.chunks), (
        "fixture must straddle"
    )
    assert any(RULING in c.text for c in result.chunks)


def test_every_span_no_longer_than_the_overlap_lies_whole_in_some_window() -> None:
    rng = random.Random(7)
    for text in generated_texts()[:60]:
        if len(text) < 2:
            continue
        overlap = 12
        result = chunk(text, 30, overlap)
        for _ in range(30):
            begin = rng.randrange(len(text))
            finish = min(len(text), begin + rng.randint(1, overlap))
            assert any(c.start <= begin and finish <= c.end for c in result.chunks)


def test_without_overlap_a_straddling_ruling_is_split_negative_control() -> None:
    text = "x " * 40 + RULING + " y" * 40
    result = chunk(text, 50, 0)
    assert not any(RULING in c.text for c in result.chunks)
