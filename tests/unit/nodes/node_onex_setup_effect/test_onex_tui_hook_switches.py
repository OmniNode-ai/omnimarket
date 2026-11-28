# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""/delegation: one hook, one switch (OMN-23413).

Operator, 2026-11-15 (screenshot of /delegation): "build separate switches",
then of the tick boxes: "bad design". Operator, 2026-11-17 (Amendment 14a):
"keep one hook for all claude prompt delegation", so the tab has one switch,
the prompt hook's; switching it off removes the hook.

Failure modes:
  H1  a paused hook is drawn as installed, or its switch on.
  H3  the switch off does not send off (it removes the hook).
  H4  a paused hook's switch turned off does not send off.
  H5  status's switch does not reach the row.
  H6  the status bar says delegation is on, unqualified, while it is paused.
"""

from __future__ import annotations

import json

import pytest
from textual.pilot import Pilot
from textual.widgets import Switch

from omnimarket.nodes.node_onex_setup_effect.cli_tui import (
    ModelTuiStatus,
    OnexTui,
    _delegation_read,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import tui_design
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui_delegation_hooks import (
    _app,
    _open,
    _text,
)

pytestmark = pytest.mark.unit

_ON = {"prompt_hook": "installed"}


def _on(hooks: dict[str, str] | None = None) -> ModelTuiStatus:
    return ModelTuiStatus(models=1, delegation="hook", hooks=hooks or _ON)


def _sent(app: OnexTui) -> list[tuple[str, ...]]:
    return [call for call in app._runner.calls if call[:1] == ("delegation",)]


async def _settle(pilot: Pilot[int]) -> None:
    for _ in range(8):
        await pilot.pause()


async def test_a_paused_hook_is_drawn_paused_and_its_switch_off() -> None:
    """H1."""
    words = tui_design().delegation
    app = _app(_on({"prompt_hook": "paused"}))
    async with app.run_test(size=(160, 46)) as pilot:
        await _open(pilot, app)
        prompt = _text(app, "#hook-prompt_hook")
        ticked = app.screen.query_one("#use-prompt_hook", Switch).value
    assert words.hook_paused in prompt
    assert ticked is False


async def test_the_switch_off_is_delegation_off() -> None:
    """H3: off removes the hook, as the old main switch did."""
    app = _app(_on())
    async with app.run_test(size=(160, 46)) as pilot:
        await _open(pilot, app)
        app.screen.query_one("#use-prompt_hook", Switch).value = False
        await pilot.pause()
        await pilot.press("ctrl+s")  # no Save button (OMN-26688)
        await _settle(pilot)
        sent = _sent(app)
    assert ("delegation", "off", "--json") in sent
    assert not any(call[1] == "on" for call in sent)


async def test_turning_off_a_paused_hook_sends_off() -> None:
    """H4."""
    app = _app(_on({"prompt_hook": "paused"}))
    async with app.run_test(size=(160, 46)) as pilot:
        await _open(pilot, app)
        app.screen.set_all(False)
        await pilot.pause()
        await pilot.press("ctrl+s")  # no Save button (OMN-26688)
        await _settle(pilot)
        sent = _sent(app)
    assert ("delegation", "off", "--json") in sent


def test_status_switch_reaches_the_hook_row() -> None:
    """H5: an installed hook whose switch is paused reads paused."""
    document = {
        "status": "on",
        "switches": {"prompts": "paused"},
        "observations": [
            {"step": "claude-hooks", "evidence": dict(_ON)},
        ],
    }
    state, hooks, problem = _delegation_read(0, json.dumps(document))
    assert (state, problem) == ("hook", None)
    assert hooks == {"prompt_hook": "paused"}


@pytest.mark.parametrize(
    ("hooks", "word"),
    [({"prompt_hook": "paused"}, "delegation_hook_paused"), (_ON, "delegation_hook")],
)
async def test_the_status_bar_says_whether_delegation_is_paused(
    hooks: dict[str, str], word: str
) -> None:
    """H6."""
    app = _app(_on(hooks))
    async with app.run_test(size=(160, 46)) as pilot:
        await _settle(pilot)
        bar = " ".join(str(app.query_one("#status").render()).split())
    assert tui_design().words[word] in bar
    if word == "delegation_hook":
        assert tui_design().words["delegation_hook_paused"] not in bar
