# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC4 (OMN-26620): the footer lists the keys of the focused screen and drops
the least important when narrow.

Drives ``KeyFooter.render``, the footer the status bar draws (review
2026-11-27: the earlier ``for_screen`` helper was used by this file only and
was removed).

Failure modes:
  AC4-F1  a key of the focused screen is missing at 140 columns.
  AC4-F2  the footer wraps (or overflows) at 40 columns instead of dropping
          the least-important keys, in the declared order.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_onex_setup_effect import cli_tui
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import tui_design

pytestmark = pytest.mark.unit


def test_footer_all_keys_at_140() -> None:
    """AC4-F1: every key of the focused screen is present at 140 columns."""
    shown = cli_tui.KeyFooter().render("config", 140).plain
    for key in tui_design().footer.screens["config"].keys:
        assert key.label in shown, (key.label, shown)


def test_footer_no_wrap_when_narrow() -> None:
    """AC4-F2: a narrow footer drops the first key of the drop order, never wraps."""
    spec = tui_design().footer.screens["config"]
    wide = cli_tui.KeyFooter().render("config", 140).plain
    narrow_width = len(wide) - 1
    shown = cli_tui.KeyFooter().render("config", narrow_width).plain
    assert "\n" not in shown, "footer must stay one line"
    assert 0 < len(shown) <= narrow_width, (len(shown), shown)
    assert spec.drop_order[0] not in shown.split(tui_design().footer.separator)
