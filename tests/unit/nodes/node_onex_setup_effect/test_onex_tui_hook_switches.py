# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""/delegation: one switch per hook (OMN-23413, operator 2026-11-15).

Operator, 2026-11-15 (screenshot of /delegation): "how to enable and disable
separate hooks?" then "build separate switches", then of the tick boxes: "bad
design". Each hook has its own switch; both off removes the hooks.

Failure modes:
  H1  a paused hook is drawn as installed, or its switch on.
  H2  unticking one hook and saving does not send on with that hook paused.
  H3  both switches off does not send off (it removes the hooks).
  H4  a paused hook's switch turned off with the other does not send off.
  H5  status's per-hook switches do not reach the rows.
  H6  the status bar says "prompts and tasks" while one is paused.
"""

from __future__ import annotations

import json

import pytest
from textual.widgets import Switch

from omnimarket.nodes.node_onex_setup_effect.cli_tui import (
    ModelTuiStatus,
    _delegation_read,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import tui_design
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import _press
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui_delegation_hooks import (
    _app,
    _open,
    _text,
)

pytestmark = pytest.mark.unit

_BOTH = {"prompt_hook": "installed", "subagent_hook": "installed"}


def _on(hooks: dict[str, str] | None = None) -> ModelTuiStatus:
    return ModelTuiStatus(models=1, delegation="hook", hooks=hooks or _BOTH)


def _sent(app) -> list[tuple[str, ...]]:  # type: ignore[no-untyped-def]
    return [call for call in app._runner.calls if call[:1] == ("delegation",)]


async def _settle(pilot) -> None:  # type: ignore[no-untyped-def]
    for _ in range(8):
        await pilot.pause()


async def test_a_paused_hook_is_drawn_paused_and_its_box_unticked() -> None:
    """H1."""
    words = tui_design().delegation
    app = _app(_on({"prompt_hook": "paused", "subagent_hook": "installed"}))
    async with app.run_test(size=(160, 46)) as pilot:
        await _open(pilot, app)
        prompt = _text(app, "#hook-prompt_hook")
        ticked = {
            key: app.screen.query_one(f"#use-{key}", Switch).value
            for key in ("prompt_hook", "subagent_hook")
        }
    assert words.hook_paused in prompt
    assert ticked == {"prompt_hook": False, "subagent_hook": True}


@pytest.mark.parametrize(
    ("key", "flag"),
    [("prompt_hook", "--no-prompts"), ("subagent_hook", "--no-subagents")],
)
async def test_unticking_one_hook_saves_on_with_it_paused(key: str, flag: str) -> None:
    """H2."""
    app = _app(_on())
    async with app.run_test(size=(160, 46)) as pilot:
        await _open(pilot, app)
        app.screen.query_one(f"#use-{key}", Switch).value = False
        await pilot.pause()
        await _press(pilot, app, "dsave")
        await _settle(pilot)
        sent = _sent(app)
    assert ("delegation", "on", "--json", flag) in sent


async def test_both_switches_off_is_delegation_off() -> None:
    """H3: both off removes the hooks, as the old main switch did."""
    app = _app(_on())
    async with app.run_test(size=(160, 46)) as pilot:
        await _open(pilot, app)
        for key in ("prompt_hook", "subagent_hook"):
            app.screen.query_one(f"#use-{key}", Switch).value = False
        await pilot.pause()
        await _press(pilot, app, "dsave")
        await _settle(pilot)
        sent = _sent(app)
    assert ("delegation", "off", "--json") in sent
    assert not any(call[1] == "on" for call in sent)


async def test_turning_off_the_last_delegating_switch_sends_off() -> None:
    """H4."""
    app = _app(_on({"prompt_hook": "paused", "subagent_hook": "installed"}))
    async with app.run_test(size=(160, 46)) as pilot:
        await _open(pilot, app)
        app.screen.set_all(False)
        await pilot.pause()
        await _press(pilot, app, "dsave")
        await _settle(pilot)
        sent = _sent(app)
    assert ("delegation", "off", "--json") in sent


def test_status_switches_reach_the_hook_rows() -> None:
    """H5: an installed hook whose switch is paused reads paused."""
    document = {
        "status": "on",
        "switches": {"prompts": "paused", "subagents": "on"},
        "observations": [
            {"step": "claude-hooks", "evidence": dict(_BOTH)},
        ],
    }
    state, hooks, problem = _delegation_read(0, json.dumps(document))
    assert (state, problem) == ("hook", None)
    assert hooks == {"prompt_hook": "paused", "subagent_hook": "installed"}


@pytest.mark.parametrize(
    ("hooks", "word"),
    [
        (
            {"prompt_hook": "paused", "subagent_hook": "installed"},
            "delegation_hook_tasks",
        ),
        (
            {"prompt_hook": "installed", "subagent_hook": "paused"},
            "delegation_hook_prompts",
        ),
        (_BOTH, "delegation_hook"),
    ],
)
async def test_the_status_bar_names_what_delegates(
    hooks: dict[str, str], word: str
) -> None:
    """H6."""
    app = _app(_on(hooks))
    async with app.run_test(size=(160, 46)) as pilot:
        await _settle(pilot)
        bar = " ".join(str(app.query_one("#status").render()).split())
    assert tui_design().words[word] in bar
