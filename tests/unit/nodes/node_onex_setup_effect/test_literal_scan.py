# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC8 (OMN-26620): no new colour, word or glyph is written in code.

Enforced by the OMN-23413 AC-27 literal scan over ``cli_tui.py``, with a
planted-word positive control: the scan MUST report a planted token in code
and pass a clean source. The fix's new presentation values (compact header
words, task-card wording, footer labels, light-theme tokens, icons) live in
``tui_design.yaml`` only, so the post-fix ``cli_tui.py`` has no new literal.

Failure mode:
  AC8-F1  the scan cannot flag a planted token (a broken instrument), or the
          shipped ``cli_tui.py`` holds a value that belongs in the design file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_onex_setup_effect import cli_tui

pytestmark = pytest.mark.unit

#: A word that is not in the design file; planted in code, the scan must flag it.
_PLANTED = "OMN26620PLANTEDTOKEN"
_NODE = Path(cli_tui.__file__).parent


def _cli_tui_source() -> str:
    return (_NODE / "cli_tui.py").read_text()


def test_literal_scan_positive_control() -> None:
    """AC8-F1: the planted-word control fires, and the shipped source is clean.

    The scan's contract: ``literal_scan(source_text)`` returns the list of
    literals that are written in code but must live in ``tui_design.yaml``
    (design words/glyphs and hex colours); an empty list means clean. The
    positive control plants a forbidden-style token to prove the instrument
    can fail. Green on dev (cli_tui.py already keeps literals in the design
    file) and green after the fix, when the gate is wired in.
    """
    scan = getattr(cli_tui, "literal_scan", None)
    assert scan is not None, (
        "AC8: no literal_scan gate in cli_tui (OMN-23413 AC27 scan)"
    )
    source = _cli_tui_source()
    # Positive control: a planted token is reported, so a real defect fails.
    planted = source + f"\n_X = '{_PLANTED}'  # planted\n"
    assert _PLANTED in scan(planted), (
        "positive control: literal_scan must flag a planted token; otherwise "
        "a real literal in code would pass the gate"
    )
    # The shipped source holds no undeclared colour, word or glyph (AC8).
    assert scan(source) == [], (
        "AC8: cli_tui.py holds a literal that belongs in tui_design.yaml"
    )
