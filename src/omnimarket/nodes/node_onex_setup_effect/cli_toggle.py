# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The delegation switch, one line like a phone's (OMN-26688).

Operator 2026-11-26, with pictures of a phone's switch: a rounded pill about
one text line tall, a round knob at one end and the word at the other ("i
want this"); a three-row pixel drawing of it was "very bad". Its ends are
the Powerline half circles where the terminal draws them (Ghostty, WezTerm
and kitty draw them themselves), and half blocks elsewhere, where those
glyphs could show as boxes.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from rich.style import Style
from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.color import Color
from textual.message import Message
from textual.widgets import Static

#: Columns the switch takes, its two ends included.
WIDTH = 12
_INSIDE = WIDTH - 2
#: The pill's ends: round where the terminal draws them, else half blocks.
ROUND = ("", "")
SQUARE = ("▐", "▌")
#: The knob: the large circle of the Nerd Font symbols where the round ends
#: are drawn (the same symbol set), else the plain dot.
KNOBS = {True: "\uf111", False: "●"}
#: Terminals that draw the Powerline half circles without a patched font.
_ROUND_PROGRAMS = ("ghostty", "wezterm")


def round_ends(environ: Mapping[str, str] | None = None) -> bool:
    """Whether this terminal draws the round ends; ONEX_TUI_ROUND_ENDS=1 or 0
    decides for one that is not known (a Nerd Font in another terminal)."""
    env = os.environ if environ is None else environ
    forced = env.get("ONEX_TUI_ROUND_ENDS")
    if forced in ("0", "1"):
        return forced == "1"
    if env.get("TERM_PROGRAM", "").lower() in _ROUND_PROGRAMS:
        return True
    return env.get("TERM", "") == "xterm-kitty"


def _inside(on: bool | None, word: str, knob_glyph: str) -> tuple[str, int | None]:
    """The cells between the ends, and where the knob is: right when on, left
    when off, none while a switch runs or the state is not read."""
    knob = {True: _INSIDE - 2, False: 1, None: None}[on]
    first, end = {True: (0, _INSIDE - 2), False: (2, _INSIDE), None: (0, _INSIDE)}[on]
    shown = word[: end - first]
    start = first + (end - first - len(shown)) // 2
    cells = [" "] * _INSIDE
    cells[start : start + len(shown)] = shown
    if knob is not None:
        cells[knob] = knob_glyph
    return "".join(cells), knob


def pill(
    on: bool | None,
    word: str,
    *,
    track: str,
    knob: str,
    ink: str,
    ends: bool | None = None,
) -> Text:
    """The switch as one line of text in the given colours; ``ends`` says
    whether its ends are round (this terminal's answer when not given)."""
    fill = Color.parse(track).rich_color
    knob_colour = Color.parse(knob).rich_color
    letters = Color.parse(ink).rich_color
    rounded = round_ends() if ends is None else ends
    left, right = ROUND if rounded else SQUARE
    cells, at = _inside(on, word, KNOBS[rounded])
    text = Text(no_wrap=True, end="")
    text.append(left, Style(color=fill))
    for x, cell in enumerate(cells):
        if x == at:
            text.append(cell, Style(color=knob_colour, bgcolor=fill))
        elif cell == " ":
            text.append(cell, Style(bgcolor=fill))
        else:
            text.append(cell, Style(color=letters, bgcolor=fill, bold=True))
    text.append(right, Style(color=fill))
    return text


class ToggleSwitch(Static, can_focus=True):
    """The switch as a widget: Enter or a click posts ``Pressed``; the screen
    decides what it switches and redraws it with ``draw``."""

    DEFAULT_CSS = f"ToggleSwitch {{ width: {WIDTH}; height: 1; }}"
    BINDINGS = [Binding("enter", "press", show=False)]

    class Pressed(Message):
        """The switch was clicked, or Enter was pressed on it."""

        def __init__(self, switch: ToggleSwitch) -> None:
            super().__init__()
            self.switch = switch

        @property
        def control(self) -> ToggleSwitch:
            return self.switch

    def __init__(self, *, id: str | None = None, name: str | None = None) -> None:
        super().__init__("", id=id, name=name)
        #: As last drawn: on, off, or ``None`` (switching, not read).
        self.on: bool | None = None
        self.drawn = Text()

    def draw(self, on: bool | None, word: str, paint: Mapping[str, str]) -> None:
        self.on = on
        self.drawn = pill(
            on, word, track=paint["track"], knob=paint["knob"], ink=paint["ink"]
        )
        self.update(self.drawn)

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.focus()
        self.post_message(self.Pressed(self))

    def action_press(self) -> None:
        self.post_message(self.Pressed(self))
