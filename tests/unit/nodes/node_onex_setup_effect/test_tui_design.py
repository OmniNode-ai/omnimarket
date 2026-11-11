# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Test that the TUI design YAML is the sole source of colours, glyphs and banner words.

Verify that a broken design file is refused on load (OMN-23413).

Failure modes:
  F1. A colour, glyph, banner word, shortcut or timing is written in code, so
      changing the design file does not change the TUI.
  F2. A design file that would draw wrongly (a letter the font lacks, a missing
      gradient, a bad colour, an alias to nothing) loads and fails on screen.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import (
    load_tui_design,
    tui_design,
)

pytestmark = pytest.mark.unit

_NODE = (
    Path(__file__).resolve().parents[4] / "src/omnimarket/nodes/node_onex_setup_effect"
)


def test_the_tui_source_holds_no_colour_or_glyph_literals() -> None:
    """F1."""
    source = (_NODE / "cli_tui.py").read_text()
    assert re.findall(r"#[0-9a-fA-F]{6}\b", source) == []
    glyphs = tui_design().glyphs
    for mark in (glyphs.task, glyphs.tip, glyphs.done, glyphs.failed):
        assert mark not in source, mark


def test_the_shipped_design_loads_and_names_every_shortcut_action() -> None:
    design = tui_design()
    assert {s.action for s in design.shortcuts} == {
        "config",
        "model",
        "delegation",
        "clear",
        "help",
        "quit",
    }
    setup = design.shortcut_for(["setup"])
    leave = design.shortcut_for(["exit"])
    assert setup is not None
    assert setup.action == "config"
    assert leave is not None
    assert leave.action == "quit"
    assert design.shortcut_for(["nope"]) is None


def _changed(tmp_path: Path, change: dict[str, Any]) -> Path:
    data = yaml.safe_load((_NODE / "tui_design.yaml").read_text())
    for dotted, value in change.items():
        target = data
        *parents, last = dotted.split(".")
        for key in parents:
            target = target[int(key)] if key.isdigit() else target[key]
        target[int(last) if last.isdigit() else last] = value
    path = tmp_path / "design.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True))
    return path


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"banner.words.0.text": "OMNIX"}, "font lacks"),
        ({"banner.words.0.gradient": "nowhere"}, "no gradient"),
        ({"theme.primary": "blue"}, "#rrggbb"),
        ({"aliases.setup": "missing"}, "names no shortcut"),
        ({"font.O": ["▄▀▀▄", "█ █", " ▀▀ "]}, "rows differ"),
        ({"min_key_length": 0}, "greater than or equal"),
        ({"glyphs.done": ""}, "at least 1 character"),
    ],
)
def test_a_design_that_would_draw_wrongly_is_refused_on_load(
    tmp_path: Path, change: dict[str, Any], message: str
) -> None:
    """F2."""
    with pytest.raises(ValidationError, match=message):
        load_tui_design(_changed(tmp_path, change))


def test_changing_the_design_file_changes_the_banner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1: the banner is drawn from whatever the file says."""
    from omnimarket.nodes.node_onex_setup_effect import cli_tui

    custom = load_tui_design(
        _changed(
            tmp_path,
            {"banner.greeting": "Hello from", "banner.tagline": "Tagline X"},
        )
    )
    monkeypatch.setattr(cli_tui, "tui_design", lambda: custom)
    text = cli_tui.welcome_banner().plain
    assert "Hello from" in text
    assert "Tagline X" in text
