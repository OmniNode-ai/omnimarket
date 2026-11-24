# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC6 (OMN-26620): Nerd Font icons are used only when switched on in
``tui_design.yaml``, and every icon has a plain-symbol fallback.

Read on dev (bf09f2968): ``glyphs`` holds plain symbols only (> ▸ ✓ · ✗) and
there is no ``icons:`` key. The mockups allow optional Nerd Font glyphs, each
with a plain fallback, gated by one setting.

Failure modes:
  AC6-F1  an icon exists with no plain-symbol fallback.
  AC6-F2  a Nerd Font glyph is emitted while the setting is off (default plain).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_onex_setup_effect import cli_tui

pytestmark = pytest.mark.unit

_DESIGN = Path(cli_tui.__file__).parent / "tui_design.yaml"


def _data() -> dict:
    return yaml.safe_load(_DESIGN.read_text())


def test_every_icon_has_fallback() -> None:
    """AC6-F1: every optional icon declares a plain-symbol fallback."""
    data = _data()
    assert "icons" in data, (
        "AC6/AC27: tui_design.yaml has no icons: key (nerd/plain switch)"
    )
    icons = data["icons"]
    assert "mode" in icons or "enabled" in icons, "no nerd/plain switch declared"
    glyphs = icons.get("glyphs", {})
    assert glyphs, "no optional icons declared"
    for name, spec in glyphs.items():
        assert spec.get("nerd"), f"{name}: no Nerd Font glyph"
        assert spec.get("fallback"), f"{name}: no plain-symbol fallback"


def test_no_nerd_glyph_when_off() -> None:
    """AC6-F2: with the switch off (default plain), no Nerd glyph is emitted."""
    data = _data()
    assert "icons" in data, "AC6: no icons: key in tui_design.yaml"
    resolve = getattr(cli_tui, "icon", None)
    assert resolve is not None, "AC6: cli_tui has no icon() resolver"
    glyphs = data["icons"].get("glyphs", {})
    assert glyphs, "no optional icons declared"
    for name, spec in glyphs.items():
        # With the default plain mode, the resolver returns the fallback.
        shown = resolve(name)
        assert shown == spec["fallback"], (name, shown, spec["fallback"])
        assert shown != spec["nerd"], (name, "Nerd glyph emitted while off")
