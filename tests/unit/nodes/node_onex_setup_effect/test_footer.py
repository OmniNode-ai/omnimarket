# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC4 (OMN-26620): the footer lists the keys of the focused screen and drops
the least important when narrow.

Read on dev (bf09f2968): the per-tab key hints live in
``tui_design.yaml:model_table.help`` and are shown as a line *inside* each tab;
there is no single footer widget that follows the focused screen, and no drop
order. The approved mockups put the keys in a one-line footer.

Failure modes:
  AC4-F1  a key of the focused screen is missing at 140 columns.
  AC4-F2  the footer wraps (or overflows) at 80 columns instead of dropping
          the least-important keys.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_onex_setup_effect import cli_tui
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import tui_design

pytestmark = pytest.mark.unit


def test_footer_all_keys_at_140() -> None:
    """AC4-F1: every key of the focused screen is present at 140 columns."""
    design = tui_design()
    assert hasattr(design, "footer"), (
        "AC4/AC27: tui_design.yaml has no footer.* keys (keys per screen, drop order)"
    )
    footer_cls = getattr(cli_tui, "KeyFooter", None)
    assert footer_cls is not None, "AC4: cli_tui has no KeyFooter widget"
    footer = footer_cls()
    shown = footer.for_screen("/config", width=140)
    for key in design.footer.screens["config"].keys:
        assert key.label in shown, (key.label, shown)


def test_footer_no_wrap_at_80() -> None:
    """AC4-F2: at 80 columns the footer drops least-important keys, never wraps."""
    design = tui_design()
    assert hasattr(design, "footer"), "AC4: no footer config in tui_design.yaml"
    footer_cls = getattr(cli_tui, "KeyFooter", None)
    assert footer_cls is not None, "AC4: cli_tui has no KeyFooter widget"
    footer = footer_cls()
    shown = footer.for_screen("/config", width=80)
    assert "\n" not in shown, "footer must stay one line"
    # It is shorter than the 140-column rendering: least-important keys dropped.
    wide = footer.for_screen("/config", width=140)
    assert len(shown) <= 80, (len(shown), shown)
    assert len(shown) < len(wide), "narrow footer must drop keys, not repeat all"
    # The drop order is declared per screen in the design file (AC27, risks).
    assert design.footer.screens["config"].drop_order, "no declared drop priority"
