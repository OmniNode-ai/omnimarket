# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""
Criterion: AC8 (OMN-26620 review, 2026-11-27): the literal scan sees glyphs and display words, and the TUI's pages hold none.

Failure modes:
- LS1: a glyph (any non-ASCII character) written in code passes the scan
- LS2: a lowercase word handed straight to the screen (Text, append, update) passes the scan
- LS3: the scan flags a docstring, a log line or a whitespace character, so it cannot be kept green honestly
- LS4: cli_tui.py, doctor_page.py or metering_page.py still holds such a literal
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_onex_setup_effect import cli_tui

pytestmark = pytest.mark.unit

_NODE: Path = Path(cli_tui.__file__).parent

_CLEAN: str = '''
def shown(text, n):
    """A docstring may say ✓ and words."""
    _LOG.warning("a log line may say words")
    gap = "\\u00a0"
    return text.append(f"{n}")
'''


@pytest.mark.parametrize(
    ("planted", "found"),
    [
        ("_X = '◆'\n", "◆"),
        ("_Y = Text('planted words')\n", "planted words"),
        ("_Z = line.append(f'{count} planted')\n", " planted"),
        ("_W = box.update('planted')\n", "planted"),
        ("_V = Text((f'{n} planted', 'x'))\n", " planted"),
    ],
)
def test_the_scan_catches_a_planted_glyph_or_display_word(
    planted: str, found: str
) -> None:
    """LS1, LS2: each planted literal is reported (the positive controls)."""
    assert found in cli_tui.literal_scan(_CLEAN + planted)


def test_the_scan_passes_docstrings_logs_and_spaces() -> None:
    """LS3."""
    assert cli_tui.literal_scan(_CLEAN) == []


@pytest.mark.parametrize(
    "name",
    ["cli_tui.py", "doctor_page.py", "metering_page.py"],
)
def test_the_pages_hold_no_literal(name: str) -> None:
    """LS4."""
    assert cli_tui.literal_scan((_NODE / name).read_text()) == []
