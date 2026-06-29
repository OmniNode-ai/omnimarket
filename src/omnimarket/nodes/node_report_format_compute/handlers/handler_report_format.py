# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""NodeReportFormatCompute — Pure compute handler for Slack Block Kit formatting.

Converts pre-fetched report markdown and structured metrics into a Slack
Block Kit blocks array with a mrkdwn fallback text string.

ONEX node type: COMPUTE — pure, deterministic, stateless, no I/O.

Block Kit hard limits enforced here:
  - MAX 50 blocks per message (Slack API limit).
  - MAX 3000 chars per section/header text element.

When the source content is truncated a "full report" link-out block is
appended (consuming one of the 50 slots).  A quiet-day payload is emitted
when the input markdown is null or empty.

This handler performs NO git, gh, Linear, or Slack I/O of any kind.
All data must be supplied by the caller (i.e. the orchestrator).
"""

from __future__ import annotations

import re
import textwrap
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_report_format_compute.models.model_report_format import (
    ModelReportFormatOutput,
)

# ---------------------------------------------------------------------------
# Block Kit hard limits (Slack API)
# ---------------------------------------------------------------------------

_MAX_BLOCKS: int = 50
_MAX_SECTION_CHARS: int = 3000
_MAX_HEADER_CHARS: int = 150  # Slack header block plain_text limit

# Reserve one slot for the link-out block when truncation occurs.
_MAX_CONTENT_BLOCKS: int = _MAX_BLOCKS - 1

# ---------------------------------------------------------------------------
# Request model
# ---------------------------------------------------------------------------


class ReportFormatRequest(BaseModel):
    """Input envelope for the report format compute handler."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    report_markdown: str | None = Field(
        default=None,
        description=(
            "Full report as a markdown string (may be null or empty on a quiet day). "
            "Sections are delimited by level-2 headings (##)."
        ),
    )
    metrics: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Optional pre-computed structured metrics dict merged into the header block."
        ),
    )
    run_date: str = Field(
        description="ISO 8601 date string (YYYY-MM-DD) for the report header.",
    )
    full_report_url: str | None = Field(
        default=None,
        description=(
            "URL linking to the complete report.  Included in a link-out block "
            "when the rendered output is truncated."
        ),
    )
    section_allowlist: list[str] | None = Field(
        default=None,
        description=(
            "Optional ordered list of section heading prefixes to include. "
            "When null all sections are included in document order."
        ),
    )


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class NodeReportFormatCompute:
    """Pure compute handler — converts report markdown → Slack Block Kit."""

    def handle(self, request: ReportFormatRequest) -> ModelReportFormatOutput:
        """Format a report into Block Kit blocks."""
        return _format_report(request)


# ---------------------------------------------------------------------------
# Public formatting entry-point (importable for tests)
# ---------------------------------------------------------------------------


def format_report(request: ReportFormatRequest) -> ModelReportFormatOutput:
    """Format a report into Block Kit blocks.

    Exported so golden-chain tests can call it directly without constructing
    the handler class.
    """
    return _format_report(request)


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------


def _format_report(request: ReportFormatRequest) -> ModelReportFormatOutput:
    markdown = request.report_markdown or ""
    is_quiet = not markdown.strip()

    if is_quiet:
        return _quiet_day_payload(request)

    sections = _split_sections(markdown)
    sections = _apply_allowlist(sections, request.section_allowlist)

    blocks: list[dict[str, Any]] = []
    truncated = False

    # Header block (date + optional metrics summary)
    header_text = _build_header_text(request.run_date, request.metrics)
    blocks.append(_header_block(header_text))

    # Divider after header
    blocks.append(_divider_block())

    section_count = 0
    for heading, body in sections:
        # Each section renders as a header + one or more section blocks.
        remaining_slots = _MAX_CONTENT_BLOCKS - len(blocks)
        if remaining_slots <= 0:
            truncated = True
            break

        section_blocks = _render_section(heading, body, remaining_slots)
        if not section_blocks:
            continue

        # If emitting these blocks would exceed the content limit, truncate.
        if len(blocks) + len(section_blocks) > _MAX_CONTENT_BLOCKS:
            truncated = True
            # Emit as many as we can.
            available = _MAX_CONTENT_BLOCKS - len(blocks)
            blocks.extend(section_blocks[:available])
            section_count += 1 if available > 0 else 0
            break

        blocks.extend(section_blocks)
        section_count += 1

    # Append link-out block when truncated.
    if truncated:
        blocks.append(_linkout_block(request.full_report_url))

    fallback_text = _build_fallback(request.run_date, request.metrics, sections)

    return ModelReportFormatOutput(
        blocks=blocks,
        fallback_text=fallback_text,
        truncated=truncated,
        block_count=len(blocks),
        section_count=section_count,
    )


def _quiet_day_payload(request: ReportFormatRequest) -> ModelReportFormatOutput:
    """Return a minimal 'quiet day' payload."""
    commit_count = 0
    if request.metrics:
        commit_count = int(request.metrics.get("total_commits", 0) or 0)

    body = (
        f":zzz:  Quiet day — {commit_count} commits."
        if commit_count > 0
        else ":zzz:  Quiet day — no activity recorded."
    )
    date_label = request.run_date
    header_text = f"Morning Report — {date_label}"

    blocks: list[dict[str, Any]] = [
        _header_block(header_text),
        _divider_block(),
        _section_text_block(body),
    ]
    return ModelReportFormatOutput(
        blocks=blocks,
        fallback_text=f"Morning Report {date_label}: {body}",
        truncated=False,
        block_count=len(blocks),
        section_count=0,
    )


# ---------------------------------------------------------------------------
# Section parsing
# ---------------------------------------------------------------------------


def _split_sections(markdown: str) -> list[tuple[str, str]]:
    """Split markdown into (heading, body) pairs on level-2 headings (##).

    A leading preamble before any ## heading is captured as a section with
    an empty heading so it appears first.
    """
    # Pattern: lines starting with '## ' delimit sections.
    pattern = re.compile(r"^## (.+)$", re.MULTILINE)
    positions = [(m.start(), m.group(1).strip()) for m in pattern.finditer(markdown)]

    if not positions:
        # No ## headings — treat the whole document as one un-headed section.
        return [("", markdown.strip())]

    sections: list[tuple[str, str]] = []

    # Preamble before first heading.
    preamble = markdown[: positions[0][0]].strip()
    if preamble:
        sections.append(("", preamble))

    for i, (start, heading) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else len(markdown)
        # Body is everything after the heading line.
        line_end = markdown.index("\n", start) if "\n" in markdown[start:] else end
        body = markdown[line_end:end].strip()
        sections.append((heading, body))

    return sections


def _apply_allowlist(
    sections: list[tuple[str, str]],
    allowlist: list[str] | None,
) -> list[tuple[str, str]]:
    """Filter sections by allowlist prefix match (case-insensitive)."""
    if allowlist is None:
        return sections
    lowered = [s.lower() for s in allowlist]
    result: list[tuple[str, str]] = []
    for heading, body in sections:
        if not heading:
            # Always include preamble / un-headed content.
            result.append((heading, body))
            continue
        if any(heading.lower().startswith(prefix) for prefix in lowered):
            result.append((heading, body))
    return result


# ---------------------------------------------------------------------------
# Block renderers
# ---------------------------------------------------------------------------


def _render_section(
    heading: str,
    body: str,
    max_blocks: int,
) -> list[dict[str, Any]]:
    """Render a single section into Block Kit blocks.

    Uses a rich_text or section block for the heading (if present) and
    splits the body into ≤3000-char section blocks.
    """
    if max_blocks <= 0:
        return []

    result: list[dict[str, Any]] = []

    if heading:
        # A section-header rendered as a bold section text (not a header block,
        # which is reserved for the top-level title). Use plain markdown bold.
        heading_text = _truncate(f"*{heading}*", _MAX_SECTION_CHARS)
        result.append(_section_text_block(heading_text))

    if body:
        # Split body into ≤3000-char chunks on word boundaries.
        chunks = _split_text(body, _MAX_SECTION_CHARS)
        for chunk in chunks:
            if len(result) >= max_blocks:
                break
            result.append(_section_text_block(chunk))

    return result[:max_blocks]


def _split_text(text: str, max_chars: int) -> list[str]:
    """Split text into chunks of at most max_chars characters.

    Splits on newlines first, then word-wraps to ensure no chunk exceeds
    max_chars.  Empty chunks are discarded.
    """
    if len(text) <= max_chars:
        return [text]

    # Use textwrap to break long lines, then re-join into max_chars chunks.
    wrapped_lines = textwrap.wrap(
        text,
        width=max_chars,
        break_long_words=True,
        replace_whitespace=False,
        expand_tabs=False,
    )

    chunks: list[str] = []
    current = ""
    for line in wrapped_lines:
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = line
    if current:
        chunks.append(current)
    return chunks or [text[:max_chars]]


def _truncate(text: str, max_chars: int) -> str:
    """Truncate text to max_chars, appending '…' if truncation occurred."""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


# ---------------------------------------------------------------------------
# Primitive block constructors
# ---------------------------------------------------------------------------


def _header_block(text: str) -> dict[str, Any]:
    return {
        "type": "header",
        "text": {
            "type": "plain_text",
            "text": _truncate(text, _MAX_HEADER_CHARS),
            "emoji": True,
        },
    }


def _divider_block() -> dict[str, Any]:
    return {"type": "divider"}


def _section_text_block(text: str) -> dict[str, Any]:
    return {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": _truncate(text, _MAX_SECTION_CHARS),
        },
    }


def _linkout_block(url: str | None) -> dict[str, Any]:
    """Return a 'Full report' link-out block.

    When no URL is available the block still notifies the reader that
    content was truncated.
    """
    if url:
        text = f":page_facing_up:  <{url}|View full report>"
    else:
        text = ":page_facing_up:  Full report available — no archive URL configured."
    return _section_text_block(text)


# ---------------------------------------------------------------------------
# Fallback text builder
# ---------------------------------------------------------------------------


def _build_header_text(run_date: str, metrics: dict[str, Any] | None) -> str:
    """Compose the top-level header text for the report."""
    base = f"Morning Report — {run_date}"
    if not metrics:
        return base
    prs = metrics.get("total_prs_merged", metrics.get("prs_merged"))
    commits = metrics.get("total_commits", metrics.get("commits"))
    parts: list[str] = []
    if prs is not None:
        parts.append(f"{prs} PRs merged")
    if commits is not None:
        parts.append(f"{commits} commits")
    if parts:
        return f"{base} | {', '.join(parts)}"
    return base


def _build_fallback(
    run_date: str,
    metrics: dict[str, Any] | None,
    sections: list[tuple[str, str]],
) -> str:
    """Build a compact mrkdwn fallback text string."""
    header = _build_header_text(run_date, metrics)
    heading_list = ", ".join(h for h, _ in sections if h)
    summary = f"{header} — {heading_list}" if heading_list else header
    return _truncate(summary, _MAX_SECTION_CHARS)
