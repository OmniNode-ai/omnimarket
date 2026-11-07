# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The simple floor page says how old its record is, in the page and in the tab title (OMN-19998).

At 03:27Z on 2026-11-06 the served record was 33 minutes old, past its 900 s bound, after the publisher was refused for a
step that did not finish within 600 s. The page already drew a banner past the bound; a reader had to be looking at the
banner to know. The age is now always on the page, and the tab title says STALE, so a tab in the background shows it too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

PAGE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_factory_floor_publish_effect/page/simple.html"
).read_text(encoding="utf-8")


def test_golden_the_page_always_shows_the_age_of_the_record_it_draws() -> None:
    assert "Record read ${ago(age)}" in PAGE
    assert "limit ${Math.floor(limit / 60)} min" in PAGE


def test_error_the_tab_title_says_stale_when_the_record_is_past_its_bound_or_the_refresh_failed() -> (
    None
):
    assert (
        'document.title = (failure || age > limit ? "STALE " : "") + "Factory floor, record "'
        in PAGE
    )


def test_error_the_banner_past_the_bound_is_still_drawn_and_the_age_is_redrawn_every_ten_seconds() -> (
    None
):
    assert "The floor is frozen: its record is ${ago(age)} old" in PAGE
    assert "setInterval(draw, 10000)" in PAGE
