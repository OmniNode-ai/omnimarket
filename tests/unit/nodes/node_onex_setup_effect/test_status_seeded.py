# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC7 (OMN-26620): the status bar shows the last known model and delegation
state at once and refreshes after.

Read on dev (bf09f2968): ``OnexTui.on_mount`` starts ``_status`` as an empty
``ModelTuiStatus()``, so ``_render_status`` shows ``status_reading``
("reading your setup…") until the async reader returns (4-12 s per the
ticket). A saved state is not seeded immediately.

Failure mode:
  AC7-F1  "reading your setup…" is shown while a saved state exists; the model
          and delegation state do not appear until the slow reader returns.
"""

from __future__ import annotations

import asyncio

import pytest
from textual.widgets import Static

from omnimarket.nodes.node_onex_setup_effect.cli_tui import (
    ModelTuiStatus,
    OnexTui,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import tui_design
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import _root, _Runner

pytestmark = pytest.mark.unit


def _status_bar(app: OnexTui) -> str:
    return " ".join(str(app.query_one("#status", Static).render()).split())


async def test_status_seeded_from_last_known() -> None:
    """AC7-F1: saved state is shown immediately, then refreshed after the reader."""
    design = tui_design()
    assert hasattr(design, "last_known") or "last_known" in dir(design), (
        "AC7/AC27: no last-known state source in tui_design.yaml"
    )
    slow = asyncio.Event()

    async def load_status() -> ModelTuiStatus:
        await slow.wait()  # a slow reader; the saved state must show before it
        return ModelTuiStatus(model="New-Model", delegation="hook")

    app = OnexTui(root=_root, runner=_Runner([]), load_status=load_status)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Seeded immediately from the last known saved state, never "reading".
        saved = design.words["status_reading"]
        bar = _status_bar(app)
        assert saved not in bar, "AC7: 'reading your setup…' shown with saved state"
        assert "Last-Model" in bar, f"no seeded model in status bar: {bar!r}"
        assert (
            design.words["delegation_off"] in bar
            or design.words["delegation_hook"] in bar
        ), "no seeded delegation state in status bar"
        slow.set()
        for _ in range(6):
            await pilot.pause()
        assert "New-Model" in _status_bar(app), (
            "status did not refresh after the reader"
        )
