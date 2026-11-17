# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""/delegation shows its hook and an unmistakable switch (OMN-23413).

Seen 2026-11-14 on the operator's screen: one switch and no word of the hooks
it installs, and a switch whose state was a small green square. Since
2026-11-17 (Amendment 14a) delegation is one hook, the prompt hook. Failure
modes:

  D1  the tab does not show the prompt hook with whether Claude Code has it
      installed.
  D2  the switch's state is not written out (ON / OFF).
  D3  a flipped but unsaved switch looks the same as the saved state.
  D4  after Save the hook rows still show the state from before.
"""

from __future__ import annotations

import json

import pytest
from textual.pilot import Pilot
from textual.widgets import Static

from omnimarket.nodes.node_onex_setup_effect.cli_tui import ModelTuiStatus, OnexTui
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import tui_design
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import (
    _press,
    _root,
    _Runner,
    _selected_rows,
    _type,
)

pytestmark = pytest.mark.unit

_ON = {"prompt_hook": "installed"}
_HALF = {"prompt_hook": "missing"}


def _app(before: ModelTuiStatus, after: ModelTuiStatus | None = None) -> OnexTui:
    """Status reads ``before`` until `onex delegation on` has run, then ``after``."""
    rows = _selected_rows(connection=False)
    rows[("delegation", "on")] = json.dumps({"status": "on"})
    runner = _Runner([], by_command=rows)

    async def status() -> ModelTuiStatus:
        switched = any(call[:2] == ("delegation", "on") for call in runner.calls)
        return after if switched and after is not None else before

    return OnexTui(root=_root, runner=runner, load_status=status)


def _state(row: str) -> str:
    """The state word a hook row shows after the hook's name (the longest that
    matches: "installed" is part of "not installed")."""
    words = tui_design().delegation
    every = (
        words.hook_missing,
        words.hook_installed,
        words.hook_unknown,
        words.hook_unread,
    )
    for word in sorted(every, key=len, reverse=True):
        if f" {word} " in f" {row} ":
            return word
    raise AssertionError(row)


async def _open(pilot: Pilot[int], app: OnexTui) -> None:
    for _ in range(4):
        await pilot.pause()
    await _type(pilot, "/delegation")
    await pilot.press("enter")
    for _ in range(6):
        await pilot.pause()


def _text(app: OnexTui, selector: str) -> str:
    return " ".join(str(app.screen.query_one(selector, Static).render()).split())


async def test_the_hook_is_shown_with_its_installed_state() -> None:
    """D1: one row, named, with what it does and its state."""
    words = tui_design().delegation
    app = _app(ModelTuiStatus(models=1, delegation="off", hooks=_HALF))
    async with app.run_test(size=(160, 46)) as pilot:
        await _open(pilot, app)
        prompt = _text(app, "#hook-prompt_hook")
        rows = len(app.screen.query(".hook"))
    assert rows == 1
    assert words.hooks[0].name in prompt
    assert _state(prompt) == words.hook_missing


@pytest.mark.parametrize(
    ("delegation", "word"), [("hook", "state_on"), ("off", "state_off")]
)
async def test_the_switch_state_is_written_out(delegation: str, word: str) -> None:
    """D2."""
    words = tui_design().delegation
    app = _app(ModelTuiStatus(models=1, delegation=delegation, hooks=_ON))
    async with app.run_test(size=(160, 46)) as pilot:
        await _open(pilot, app)
        state = _text(app, "#on-state")
    assert getattr(words, word) in state
    assert words.state_pending not in state


async def test_an_unsaved_flip_says_save_applies_it() -> None:
    """D3."""
    words = tui_design().delegation
    app = _app(ModelTuiStatus(models=1, delegation="off", hooks=_HALF))
    async with app.run_test(size=(160, 46)) as pilot:
        await _open(pilot, app)
        app.screen.set_all(True)
        await pilot.pause()
        state = _text(app, "#on-state")
    assert words.state_on in state
    assert words.state_pending in state


async def test_after_save_the_hook_rows_show_the_new_state() -> None:
    """D4: Save runs onex delegation on, reads status again and redraws."""
    words = tui_design().delegation
    app = _app(
        ModelTuiStatus(models=1, delegation="off", hooks=_HALF),
        ModelTuiStatus(models=1, delegation="hook", hooks=_ON),
    )
    async with app.run_test(size=(160, 46)) as pilot:
        await _open(pilot, app)
        app.screen.set_all(True)
        await _press(pilot, app, "dsave")
        for _ in range(8):
            await pilot.pause()
        prompt = _text(app, "#hook-prompt_hook")
        state = _text(app, "#on-state")
    assert _state(prompt) == words.hook_installed
    assert words.state_on in state
    assert words.state_pending not in state
