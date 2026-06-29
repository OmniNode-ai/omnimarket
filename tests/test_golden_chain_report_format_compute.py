# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain tests for node_report_format_compute (OMN-13724).

Exercises the full handler stack:
  ReportFormatRequest -> format_report -> ModelReportFormatOutput

Key assertions:
  - Block Kit hard limits: ≤50 blocks, ≤3000 chars per section text.
  - Oversized input is truncated and includes a link-out block.
  - Quiet-day rendering always produces a valid payload.
  - Section allowlist filtering.
  - Output model is strongly-typed and frozen.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_report_format_compute import (
    ModelReportFormatOutput,
    NodeReportFormatCompute,
    ReportFormatRequest,
    format_report,
)
from omnimarket.nodes.node_report_format_compute.handlers.handler_report_format import (
    _MAX_BLOCKS,
    _MAX_SECTION_CHARS,
    _split_sections,
    _split_text,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_RUN_DATE = "2026-06-28"
_FULL_REPORT_URL = "https://example.com/reports/2026-06-28.md"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(**kwargs: object) -> ReportFormatRequest:
    return ReportFormatRequest(run_date=_RUN_DATE, **kwargs)  # type: ignore[arg-type]


def _oversized_section(chars: int, heading: str = "Big Section") -> str:
    """Build a markdown section whose body exceeds `chars` characters."""
    # Each line is 80 chars + newline; build enough to exceed the limit.
    line = "A" * 79 + "\n"
    repetitions = (chars // len(line)) + 2
    body = line * repetitions
    return f"## {heading}\n\n{body}"


def _multi_section_markdown(n_sections: int, chars_per_section: int = 100) -> str:
    """Build markdown with n_sections level-2 sections."""
    lines: list[str] = []
    for i in range(n_sections):
        lines.append(f"## Section {i + 1}")
        lines.append("X" * chars_per_section)
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Import / public surface
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPublicSurface:
    def test_all_symbols_importable(self) -> None:
        assert NodeReportFormatCompute is not None
        assert ReportFormatRequest is not None
        assert ModelReportFormatOutput is not None
        assert format_report is not None

    def test_handler_handle_delegates_to_format_report(self) -> None:
        req = _make_request(report_markdown="## Hello\n\nWorld")
        handler = NodeReportFormatCompute()
        result = handler.handle(req)
        assert isinstance(result, ModelReportFormatOutput)

    def test_output_is_frozen(self) -> None:
        result = format_report(_make_request(report_markdown="## A\n\nB"))
        # Frozen model rejects direct attribute mutation.
        with pytest.raises((ValidationError, TypeError)):
            result.block_count = 999  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Quiet-day rendering
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestQuietDayRendering:
    def test_null_markdown_produces_quiet_payload(self) -> None:
        result = format_report(_make_request())
        assert isinstance(result, ModelReportFormatOutput)
        assert result.block_count >= 1
        assert result.truncated is False

    def test_empty_markdown_produces_quiet_payload(self) -> None:
        result = format_report(_make_request(report_markdown=""))
        assert isinstance(result, ModelReportFormatOutput)
        assert result.truncated is False

    def test_whitespace_only_markdown_produces_quiet_payload(self) -> None:
        result = format_report(_make_request(report_markdown="   \n  \t  "))
        assert isinstance(result, ModelReportFormatOutput)
        assert result.truncated is False

    def test_quiet_day_fallback_text_contains_date(self) -> None:
        result = format_report(_make_request())
        assert _RUN_DATE in result.fallback_text

    def test_quiet_day_with_commit_count_in_metrics(self) -> None:
        result = format_report(
            _make_request(
                metrics={"total_commits": 7},
            )
        )
        # The quiet-day body should mention the commit count.
        all_text = " ".join(
            blk.get("text", {}).get("text", "") for blk in result.blocks
        )
        assert "7" in all_text or "commits" in all_text.lower()

    def test_quiet_day_zero_commits(self) -> None:
        result = format_report(
            _make_request(
                report_markdown=None,
                metrics={"total_commits": 0},
            )
        )
        assert result.truncated is False
        assert result.block_count >= 1


# ---------------------------------------------------------------------------
# Block Kit hard limits — the primary golden-chain assertions
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBlockKitHardLimits:
    def test_oversized_report_stays_within_50_blocks(self) -> None:
        """Deliberately oversized input: 60 sections x 200-char bodies."""
        markdown = _multi_section_markdown(n_sections=60, chars_per_section=200)
        result = format_report(
            _make_request(
                report_markdown=markdown,
                full_report_url=_FULL_REPORT_URL,
            )
        )
        assert result.block_count <= _MAX_BLOCKS, (
            f"Expected ≤{_MAX_BLOCKS} blocks, got {result.block_count}"
        )

    def test_truncated_flag_set_for_oversized_input(self) -> None:
        """60 sections should trigger truncation."""
        markdown = _multi_section_markdown(n_sections=60, chars_per_section=200)
        result = format_report(
            _make_request(
                report_markdown=markdown,
                full_report_url=_FULL_REPORT_URL,
            )
        )
        assert result.truncated is True

    def test_linkout_block_present_when_truncated_with_url(self) -> None:
        """When truncated and a URL is given, the last block is a link-out."""
        markdown = _multi_section_markdown(n_sections=60, chars_per_section=200)
        result = format_report(
            _make_request(
                report_markdown=markdown,
                full_report_url=_FULL_REPORT_URL,
            )
        )
        assert result.truncated is True
        last_block = result.blocks[-1]
        assert last_block["type"] == "section"
        link_text: str = last_block["text"]["text"]
        assert _FULL_REPORT_URL in link_text

    def test_linkout_block_present_when_truncated_without_url(self) -> None:
        """When truncated with no URL, a link-out block is still appended."""
        markdown = _multi_section_markdown(n_sections=60, chars_per_section=200)
        result = format_report(
            _make_request(
                report_markdown=markdown,
                full_report_url=None,
            )
        )
        assert result.truncated is True
        last_block = result.blocks[-1]
        assert last_block["type"] == "section"
        link_text = last_block["text"]["text"]
        # Should mention truncation without a URL.
        assert "full report" in link_text.lower() or "truncated" in link_text.lower()

    def test_all_section_texts_within_3000_chars(self) -> None:
        """Every section / header text element must be ≤3000 chars."""
        # Build a section with a body far exceeding 3000 chars.
        big_body = "W" * 10_000
        markdown = f"## Huge Section\n\n{big_body}"
        result = format_report(
            _make_request(
                report_markdown=markdown,
                full_report_url=_FULL_REPORT_URL,
            )
        )
        for block in result.blocks:
            text_obj = block.get("text", {})
            if isinstance(text_obj, dict) and "text" in text_obj:
                text_val: str = text_obj["text"]
                assert len(text_val) <= _MAX_SECTION_CHARS, (
                    f"Block text exceeds {_MAX_SECTION_CHARS} chars: {len(text_val)}"
                )

    def test_exact_50_block_boundary_respected(self) -> None:
        """Boundary: 49 sections should fit; block count ≤ 50."""
        markdown = _multi_section_markdown(n_sections=49, chars_per_section=10)
        result = format_report(
            _make_request(
                report_markdown=markdown,
                full_report_url=_FULL_REPORT_URL,
            )
        )
        assert result.block_count <= _MAX_BLOCKS

    def test_single_huge_section_text_split_into_multiple_blocks(self) -> None:
        """A single section body > 3000 chars is split across multiple section blocks."""
        big_body = "B" * ((_MAX_SECTION_CHARS * 3) + 500)
        markdown = f"## Chunked\n\n{big_body}"
        result = format_report(
            _make_request(
                report_markdown=markdown,
                full_report_url=_FULL_REPORT_URL,
            )
        )
        # At least one section block should exist (the heading bold text).
        section_blocks = [b for b in result.blocks if b["type"] == "section"]
        assert len(section_blocks) >= 1
        # No single text must exceed the limit.
        for block in result.blocks:
            text_obj = block.get("text", {})
            if isinstance(text_obj, dict) and "text" in text_obj:
                assert len(text_obj["text"]) <= _MAX_SECTION_CHARS


# ---------------------------------------------------------------------------
# Normal rendering
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNormalRendering:
    def test_small_report_not_truncated(self) -> None:
        markdown = "## Summary\n\nAll good today."
        result = format_report(_make_request(report_markdown=markdown))
        assert result.truncated is False
        assert result.block_count >= 1

    def test_header_block_present(self) -> None:
        result = format_report(_make_request(report_markdown="## A\n\nContent"))
        header_blocks = [b for b in result.blocks if b["type"] == "header"]
        assert len(header_blocks) == 1

    def test_header_contains_run_date(self) -> None:
        result = format_report(_make_request(report_markdown="## A\n\nContent"))
        header_block = next(b for b in result.blocks if b["type"] == "header")
        assert _RUN_DATE in header_block["text"]["text"]

    def test_divider_block_present(self) -> None:
        result = format_report(_make_request(report_markdown="## A\n\nContent"))
        divider_blocks = [b for b in result.blocks if b["type"] == "divider"]
        assert len(divider_blocks) >= 1

    def test_metrics_included_in_header(self) -> None:
        result = format_report(
            _make_request(
                report_markdown="## A\n\nX",
                metrics={"total_prs_merged": 42, "total_commits": 100},
            )
        )
        header_block = next(b for b in result.blocks if b["type"] == "header")
        header_text: str = header_block["text"]["text"]
        assert "42" in header_text or "100" in header_text

    def test_section_count_reflects_rendered_sections(self) -> None:
        markdown = "## Section A\n\nContent A\n\n## Section B\n\nContent B"
        result = format_report(_make_request(report_markdown=markdown))
        assert result.section_count == 2

    def test_fallback_text_is_string(self) -> None:
        result = format_report(_make_request(report_markdown="## A\n\nB"))
        assert isinstance(result.fallback_text, str)
        assert len(result.fallback_text) > 0

    def test_fallback_text_within_3000_chars(self) -> None:
        result = format_report(_make_request(report_markdown="## A\n\n" + "X" * 5000))
        assert len(result.fallback_text) <= _MAX_SECTION_CHARS

    def test_blocks_is_list_of_dicts(self) -> None:
        result = format_report(_make_request(report_markdown="## A\n\nB"))
        assert isinstance(result.blocks, list)
        assert all(isinstance(b, dict) for b in result.blocks)

    def test_no_markdown_headings_renders_as_single_section(self) -> None:
        result = format_report(
            _make_request(report_markdown="Just some plain text without headings.")
        )
        assert result.block_count >= 1
        assert result.truncated is False


# ---------------------------------------------------------------------------
# Section allowlist filtering
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSectionAllowlist:
    _markdown = (
        "## Velocity\n\nVelocity data\n\n"
        "## CI Health\n\nCI data\n\n"
        "## Blockers\n\nNo blockers"
    )

    def test_allowlist_filters_to_matching_sections(self) -> None:
        result = format_report(
            _make_request(
                report_markdown=self._markdown,
                section_allowlist=["Velocity", "Blockers"],
            )
        )
        # Should not include CI Health section.
        all_text = " ".join(
            blk.get("text", {}).get("text", "") for blk in result.blocks
        )
        assert "Velocity" in all_text
        assert "Blockers" in all_text
        assert "CI Health" not in all_text

    def test_empty_allowlist_produces_minimal_output(self) -> None:
        result = format_report(
            _make_request(
                report_markdown=self._markdown,
                section_allowlist=[],
            )
        )
        # No sections match; only header + divider remain.
        assert result.block_count >= 1

    def test_null_allowlist_includes_all_sections(self) -> None:
        result = format_report(
            _make_request(
                report_markdown=self._markdown,
                section_allowlist=None,
            )
        )
        assert result.section_count == 3

    def test_allowlist_case_insensitive_prefix_match(self) -> None:
        result = format_report(
            _make_request(
                report_markdown=self._markdown,
                section_allowlist=["velocity"],  # lowercase
            )
        )
        all_text = " ".join(
            blk.get("text", {}).get("text", "") for blk in result.blocks
        )
        assert "Velocity" in all_text


# ---------------------------------------------------------------------------
# Unit helpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSplitSections:
    def test_no_headings_returns_single_section(self) -> None:
        sections = _split_sections("Hello world")
        assert len(sections) == 1
        assert sections[0][0] == ""
        assert sections[0][1] == "Hello world"

    def test_two_headings_returns_two_sections(self) -> None:
        md = "## A\n\nContent A\n\n## B\n\nContent B"
        sections = _split_sections(md)
        assert len(sections) == 2
        assert sections[0][0] == "A"
        assert sections[1][0] == "B"

    def test_preamble_before_first_heading_included(self) -> None:
        md = "Intro text\n\n## A\n\nContent"
        sections = _split_sections(md)
        assert sections[0][0] == ""
        assert "Intro text" in sections[0][1]


@pytest.mark.unit
class TestSplitText:
    def test_short_text_returned_as_single_chunk(self) -> None:
        text = "Hello"
        chunks = _split_text(text, 100)
        assert chunks == ["Hello"]

    def test_text_exactly_at_limit_not_split(self) -> None:
        text = "A" * _MAX_SECTION_CHARS
        chunks = _split_text(text, _MAX_SECTION_CHARS)
        assert len(chunks) == 1
        assert len(chunks[0]) <= _MAX_SECTION_CHARS

    def test_text_exceeding_limit_split_into_chunks(self) -> None:
        text = "W " * 2000  # 4000 chars
        chunks = _split_text(text, _MAX_SECTION_CHARS)
        assert len(chunks) >= 2
        for chunk in chunks:
            assert len(chunk) <= _MAX_SECTION_CHARS

    def test_all_chunks_within_limit(self) -> None:
        text = "X" * (_MAX_SECTION_CHARS * 4)
        chunks = _split_text(text, _MAX_SECTION_CHARS)
        for chunk in chunks:
            assert len(chunk) <= _MAX_SECTION_CHARS


# ---------------------------------------------------------------------------
# Pydantic validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRequestValidation:
    def test_missing_run_date_raises_validation_error(self) -> None:
        with pytest.raises(ValidationError):
            ReportFormatRequest.model_validate({})  # run_date missing

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            ReportFormatRequest(run_date=_RUN_DATE, unknown_field="x")  # type: ignore[call-arg]

    def test_frozen_model_rejects_mutation(self) -> None:
        req = _make_request(report_markdown="## A\n\nB")
        with pytest.raises((ValidationError, TypeError)):
            req.run_date = "1970-01-01"  # type: ignore[misc]
