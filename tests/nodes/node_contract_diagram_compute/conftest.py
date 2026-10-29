# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared helpers: the fixture copy of the plan's diagrams and a reader that does not use the node."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures"
PLAN_DIAGRAMS = FIXTURE_DIR / "plan_diagrams.md"

DIAGRAM_MARKER = re.compile(
    r"^<!-- diagram-contract: (\S+?):(\S+?)(?: (collapsed))? -->$"
)
FRAGMENT_MARKER = re.compile(r"^<!-- contract-fragment: (\S+?):(\S+?) -->$")


@pytest.fixture
def plan_text() -> str:
    return PLAN_DIAGRAMS.read_text(encoding="utf-8")


def fenced_after(lines: list[str], marker_index: int) -> tuple[int, int]:
    """Indexes of the opening and closing fence of the block after the marker at ``marker_index``."""
    start = marker_index + 1
    while not lines[start].startswith("```"):
        start += 1
    end = start + 1
    while not lines[end].startswith("```"):
        end += 1
    return start, end


def diagram_blocks(text: str) -> list[tuple[str, str, bool, list[str]]]:
    """Every (node, block, collapsed, mermaid lines) of the document, read without the node."""
    lines = text.splitlines()
    found = []
    for index, line in enumerate(lines):
        match = DIAGRAM_MARKER.match(line)
        if match:
            start, end = fenced_after(lines, index)
            found.append(
                (match[1], match[2], match[3] == "collapsed", lines[start + 1 : end])
            )
    return found


def fragment_blocks(text: str) -> dict[str, str]:
    """Every fragment of the document by ``node:block`` id, read without the node."""
    lines = text.splitlines()
    found = {}
    for index, line in enumerate(lines):
        match = FRAGMENT_MARKER.match(line)
        if match:
            start, end = fenced_after(lines, index)
            found[f"{match[1]}:{match[2]}"] = "\n".join(lines[start + 1 : end]) + "\n"
    return found
