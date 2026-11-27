# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC7 (OMN-26620): the status bar shows the last known model and delegation
state at once and refreshes after.

Failure modes:
  AC7-F1  "reading your setup…" is shown while a saved state exists; the model
          and delegation state do not appear until the slow reader returns.
  AC7-F2  with nothing saved, the bar shows a made-up model name (demo
          2026-11-25: "Last-Model first" for 2-3 s on every start).
  AC7-F3  a read that lands is not kept, so the next start cannot seed from it.
"""

from __future__ import annotations

import asyncio

import pytest
from textual.widgets import Static

from omnimarket.nodes.node_onex_setup_effect.cli_tui import (
    ModelTuiStatus,
    OnexTui,
    StatusStore,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import tui_design
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import _root, _Runner

pytestmark = pytest.mark.unit


def _status_bar(app: OnexTui) -> str:
    return " ".join(str(app.query_one("#status", Static).render()).split())


def _slow(model: str = "New-Model") -> tuple[asyncio.Event, object]:
    gate = asyncio.Event()

    async def load_status() -> ModelTuiStatus:
        await gate.wait()
        return ModelTuiStatus(model=model, delegation="hook", models=2)

    return gate, load_status


async def test_status_seeded_from_last_known() -> None:
    """AC7-F1: the saved state is shown immediately, then refreshed after the reader."""
    design = tui_design()
    StatusStore.save(ModelTuiStatus(model="Saved-Model", delegation="off"))
    gate, load_status = _slow()
    app = OnexTui(root=_root, runner=_Runner([]), load_status=load_status)
    async with app.run_test() as pilot:
        await pilot.pause()
        bar = _status_bar(app)
        assert design.words["status_reading"] not in bar
        assert "Saved-Model" in bar, bar
        assert design.words["delegation_off"] in bar, bar
        gate.set()
        for _ in range(6):
            await pilot.pause()
        assert "New-Model" in _status_bar(app)


async def test_nothing_saved_shows_reading_never_a_made_up_model() -> None:
    """AC7-F2."""
    design = tui_design()
    gate, load_status = _slow()
    app = OnexTui(root=_root, runner=_Runner([]), load_status=load_status)
    async with app.run_test() as pilot:
        await pilot.pause()
        bar = _status_bar(app)
        assert "Last-Model" not in bar, bar
        assert design.words["status_reading"] in bar, bar
        gate.set()


async def test_a_landed_read_is_kept_for_the_next_start() -> None:
    """AC7-F3."""
    gate, load_status = _slow("Kept-Model")
    app = OnexTui(root=_root, runner=_Runner([]), load_status=load_status)
    async with app.run_test() as pilot:
        gate.set()
        for _ in range(6):
            await pilot.pause()
    assert StatusStore.load().model == "Kept-Model"
    assert StatusStore.load().delegation == "hook"
