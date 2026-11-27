"""OMN-26620: the welcome from a saved setup (80x24 sweep, 2026-11-25).

W1  with Auto saved, the welcome still says "reading your setup…" until the
    CLI answers (about 3 s), though the status bar already shows the setup.
W2  with Auto, the header's second row reads "   · ~/folder": an empty model
    slot and a separator with nothing before it.
W3  before anything is known, the header still shows a separator with
    nothing before it.
"""

from __future__ import annotations

import asyncio

from textual.widgets import Static

from omnimarket.nodes.node_onex_setup_effect import cli_tui
from omnimarket.nodes.node_onex_setup_effect.cli_tui import (
    ModelTuiStatus,
    OnexTui,
    StatusStore,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import tui_design
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import _root, _Runner


async def _never() -> ModelTuiStatus:
    await asyncio.Event().wait()
    raise AssertionError


async def test_a_saved_setup_fills_the_welcome_at_once() -> None:
    """W1."""
    StatusStore.save(ModelTuiStatus(model=None, delegation="off", models=3))
    app = OnexTui(root=_root, runner=_Runner([]), load_status=_never)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        summary = str(app.query_one("#summary", Static).render())
        assert tui_design().words["status_reading"] not in summary, summary
        assert "3 models" in summary, summary


def _second_row(model: str | None) -> str:
    return cli_tui.header_renderable(model, 78).plain.splitlines()[1]


def test_auto_is_named_in_the_header() -> None:
    """W2."""
    row = _second_row(tui_design().words["header_auto"])
    assert "Auto · " in row, row


def test_an_unknown_model_leaves_no_dangling_separator() -> None:
    """W3."""
    row = _second_row(None)
    after_mark = row[len(tui_design().wordmark.rows[1]) :].lstrip()
    assert not after_mark.startswith("·"), row
