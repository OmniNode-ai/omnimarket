# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Delegation tab lists the agents onex delegates from, one line each with
its own switch (OMN-26688).

Operator 2026-11-26, with pictures of an agent list: "do like this", then "i
want this" (a name and a switch per line); "in delegation page we don't need
to show the models because we have model tab already"; the agents whose
integration comes later still get a switch that works, marked with one
symbol whose meaning is written once at the bottom; and the tab's window is
not to differ in size from Config, Model and Dashboard.

Failure modes:
  A1  an agent is missing or out of order, or a row is more than one line.
  A2  the models table is still on the tab.
  A3  an agent whose integration is in progress lacks the mark, Claude Code
      has it, the old words repeat on every row, or the mark is not
      explained at the bottom.
  A4  the count is not "<on>/<all> enabled", or does not follow a switch.
  A5  Down does not reach the next agent's switch, or Space or a click there
      switches Claude Code (sends a delegation command) instead.
  A6  a switched agent is not remembered: the next tab shows it off.
  A7  switching an agent in progress on gives no note that it does nothing yet.
  A8  a switch is not the one-line pill, or its knob is on the wrong side, or
      it is not in the open theme's colours.
  A9  the tab's window is a different size from the other tabs'.
  A11 a switch touches the list's scrollbar: two clear columns between.
  A10 the row whose switch has the keyboard is not marked (its name bold in
      the accent colour), or a row without it is.
"""

from __future__ import annotations

import json

import pytest
from textual.widgets import Static

from omnimarket.nodes.node_onex_setup_effect import cli_tui
from omnimarket.nodes.node_onex_setup_effect.cli_toggle import WIDTH, ToggleSwitch, pill
from tests.unit.nodes.node_onex_setup_effect import tabs_redesign_support as sup
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import _Runner

pytestmark = pytest.mark.unit

AGENTS = [
    "Claude Code",
    "Codex CLI",
    "OpenCode",
    "Pi",
    "Cursor CLI",
    "Droid CLI",
    "Hermes Agent",
    "GitHub Copilot CLI",
    "Google Antigravity CLI",
    "Kilo Code",
    "Kiro CLI",
    "Goose",
    "Cline CLI",
    "Command Code",
    "Mistral Vibe CLI",
    "Kimi Code CLI",
    "Qwen Code",
    "Amp",
    "DeepSeek Harness",
    "Oh My Pi (OMP)",
]
MARK = "ⓘ"
IN_PROGRESS = "integration in progress"


def _texts(app: cli_tui.OnexTui, selector: str) -> list[str]:
    return [
        str(widget.render()) for widget in app.screen.query(selector).results(Static)
    ]


def _count(app: cli_tui.OnexTui) -> str:
    return str(app.screen.query_one("#dcount", Static).render())


@pytest.mark.parametrize("size", sup.SIZES)
async def test_the_agents_are_listed_one_line_each(size: tuple[int, int]) -> None:
    """A1, A3."""
    app = sup.app()
    async with app.run_test(size=size) as pilot:
        await sup.open_tab(pilot, app, "delegation")
        names = _texts(app, ".agent-name")
        heights = {row.outer_size.height for row in app.screen.query(".agent-row")}
        legend = str(app.screen.query_one("#dlegend", Static).render())
        title = str(app.screen.query_one("#dtitle", Static).render())
        text = sup.screen_text(app)
    assert [name.replace(MARK, "").strip() for name in names] == AGENTS, names
    assert heights == {1}, heights
    assert title == "Agent delegation", title
    for agent, name in zip(AGENTS, names, strict=True):
        assert (MARK in name) is (agent != "Claude Code"), name
    assert legend == f"{MARK} = {IN_PROGRESS}", legend
    assert text.count(IN_PROGRESS) == 1, text  # said once, at the bottom


@pytest.mark.parametrize("size", sup.SIZES)
async def test_no_models_table_on_the_delegation_tab(size: tuple[int, int]) -> None:
    """A2."""
    app = sup.app()
    async with app.run_test(size=size) as pilot:
        await sup.open_tab(pilot, app, "delegation")
        tables = list(app.screen.query("ModelTable"))
        text = sup.screen_text(app)
    assert tables == [], tables
    for model_word in ("Tried in this order", "qwen3", "gemini", "In $/1M"):
        assert model_word not in text, (model_word, text)


@pytest.mark.parametrize(("delegation", "first"), [("hook", 1), ("off", 0)])
async def test_the_count_follows_the_switches(delegation: str, first: int) -> None:
    """A4."""
    app = sup.app(delegation=delegation)
    async with app.run_test(size=(140, 40)) as pilot:
        await sup.open_tab(pilot, app, "delegation")
        shown = _count(app)
        await pilot.press("down", "space")
        await sup.settle(pilot)
        after = _count(app)
    assert shown == f"{first}/{len(AGENTS)} enabled", shown
    assert after == f"{first + 1}/{len(AGENTS)} enabled", after


@pytest.mark.parametrize("how", ["space", "click"])
async def test_an_agent_in_progress_switches_by_itself(how: str) -> None:
    """A5, A6, A7: Codex CLI switches on, Claude Code and its hook are left
    alone, the note says the integration is in progress, and a new tab shows
    Codex CLI on."""
    run = _Runner([])
    app = sup.app(run)
    async with app.run_test(size=(140, 40)) as pilot:
        await sup.open_tab(pilot, app, "delegation")
        if how == "space":
            await pilot.press("down")
            focused = app.screen.focused
            assert isinstance(focused, ToggleSwitch), focused
            assert focused.name == "codex", focused
            await pilot.press("space")
        else:
            await pilot.click("#agent-codex")
        await sup.settle(pilot)
        codex = app.screen.query_one("#agent-codex", ToggleSwitch)
        claude = app.screen.query_one("#dturn", ToggleSwitch)
        note = str(app.screen.query_one("#dnote", Static).render())
    assert codex.on is True, codex.on
    assert claude.on is True, claude.on
    switched = [
        c for c in run.calls if c[:1] == ("delegation",) and c[1] in ("on", "off")
    ]
    assert switched == [], run.calls
    assert "Codex CLI" in note, note
    assert IN_PROGRESS in note, note
    assert json.loads(cli_tui.AgentStore._path.read_text()) == {"codex": True}

    again = sup.app()
    async with again.run_test(size=(140, 40)) as pilot:
        await sup.open_tab(pilot, again, "delegation")
        assert again.screen.query_one("#agent-codex", ToggleSwitch).on is True


@pytest.mark.parametrize("theme", ["onex-dark", "onex-light"])
async def test_each_switch_is_the_pill_in_the_theme_colours(theme: str) -> None:
    """A8."""
    app = sup.app()
    async with app.run_test(size=(140, 40)) as pilot:
        app.theme = theme
        await sup.open_tab(pilot, app, "delegation")
        await sup.settle(pilot)
        found = app.get_css_variables()
        screen = app.screen
        assert isinstance(screen, cli_tui.DelegationScreen)
        switches = list(screen.query(ToggleSwitch))
        drawn = {switch.name: switch for switch in switches}
        assert len(switches) == len(AGENTS), [s.name for s in switches]
        for switch in switches:
            assert (switch.outer_size.width, switch.outer_size.height) == (WIDTH, 1)
            word = "ON" if switch.on else "OFF"
            assert (
                switch.drawn.plain
                == pill(switch.on, word, track="#000", knob="#000", ink="#000").plain
            ), switch.name
        claude, codex = drawn["claude_code"], drawn["codex"]
        on_paint, off_paint = screen.toggle_paint(True), screen.toggle_paint(False)
    assert claude.on is True
    assert codex.on is False
    assert "ON" in claude.drawn.plain
    assert "OFF" in codex.drawn.plain
    assert on_paint["track"] == found["toggle-on"], on_paint
    assert on_paint["knob"] == on_paint["ink"] == found["success"], on_paint
    assert off_paint["track"] == found["toggle-off"], off_paint
    assert off_paint["ink"] == found["muted"], off_paint
    assert off_paint["knob"] not in (off_paint["ink"], off_paint["track"]), off_paint


@pytest.mark.parametrize("size", [(200, 50), (80, 24)])
async def test_the_window_is_the_size_of_the_other_tabs(size: tuple[int, int]) -> None:
    """A9."""
    app = sup.app()
    boxes: dict[str, tuple[int, int]] = {}
    async with app.run_test(size=size) as pilot:
        for tab in ("config", "model", "delegation", "dashboard"):
            await sup.open_tab(pilot, app, tab)
            box = app.screen.query_one("#deleg, #form, #pick, #board")
            boxes[tab] = (box.outer_size.width, box.outer_size.height)
            app.pop_screen()
            await sup.settle(pilot, 3)
    assert len(set(boxes.values())) == 1, boxes


async def test_the_row_with_the_keyboard_is_marked() -> None:
    """A10."""
    app = sup.app()
    async with app.run_test(size=(140, 40)) as pilot:
        await sup.open_tab(pilot, app, "delegation")
        primary = app.get_css_variables()["primary"]

        def marked() -> list[str]:
            return [
                row.id or ""
                for row in app.screen.query(".agent-row")
                if row.has_class("-current")
            ]

        def name_colour(row: str) -> str:
            name = app.screen.query_one(f"#{row} .agent-name", Static)
            return name.styles.color.hex.lower()

        first = marked()
        first_colour = name_colour("row-claude_code")
        await pilot.press("down", "down")
        await sup.settle(pilot)
        moved = marked()
        moved_colour = name_colour("row-opencode")
        left_colour = name_colour("row-claude_code")
    assert first == ["row-claude_code"], first
    assert moved == ["row-opencode"], moved
    assert first_colour == moved_colour == primary.lower(), (first_colour, primary)
    assert left_colour != primary.lower(), left_colour


@pytest.mark.parametrize("size", [*sup.SIZES, (173, 42)])
async def test_the_switches_keep_clear_of_the_scrollbar(size: tuple[int, int]) -> None:
    """A11."""
    app = sup.app()
    async with app.run_test(size=size) as pilot:
        await sup.open_tab(pilot, app, "delegation")
        agents = app.screen.query_one("#agents")
        bar = agents.vertical_scrollbar.region.x
        rights = {switch.region.right for switch in app.screen.query(ToggleSwitch)}
    assert agents.show_vertical_scrollbar, size
    assert max(rights) <= bar - 2, (rights, bar)
