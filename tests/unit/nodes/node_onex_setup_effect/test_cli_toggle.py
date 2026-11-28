# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The delegation switch is one line, like a phone's (OMN-26688).

Operator 2026-11-26, with pictures of a phone's switch (a rounded pill about
one text line tall, a round knob at one end, the word at the other): "i want
this". A three-row pixel drawing of it was "very bad".

Failure modes:
  P1  the knob is not on the side the switch is set to: ON right, OFF left;
      or a switch neither on nor off (switching, not read) has a knob.
  P2  the ends are not round half circles on a terminal that draws them, or
      are those glyphs on one that may not (they would show as boxes).
  P3  the word is cut, not inside the pill, or sits on the knob.
  P4  a state draws wider or narrower than the switch, or on two lines.
  P5  a cell takes the wrong colour: ends, track, knob or word.
  P6  the terminal check is wrong: Ghostty, WezTerm and kitty draw the half
      circles, Apple Terminal does not, and ONEX_TUI_ROUND_ENDS decides.
  P7  where the round ends are drawn the knob is not the large circle of the
      same symbol set, or elsewhere it is not the plain dot.
"""

from __future__ import annotations

import pytest
from rich.color import Color as RichColor
from rich.console import Console

from omnimarket.nodes.node_onex_setup_effect.cli_toggle import (
    KNOBS,
    ROUND,
    SQUARE,
    WIDTH,
    pill,
    round_ends,
)

pytestmark = pytest.mark.unit

_PAINT = {"track": "#24402c", "knob": "#e0ffe0", "ink": "#aaffaa"}
_STATES = [(True, "ON"), (False, "OFF"), (None, "switching…"), (None, "not read")]


@pytest.mark.parametrize(("on", "word"), _STATES)
def test_the_knob_is_on_the_switched_side(on: bool | None, word: str) -> None:
    """P1."""
    drawn = pill(on, word, ends=True, **_PAINT).plain
    knob = KNOBS[True]
    if on is None:
        assert knob not in drawn, drawn
        return
    at = drawn.index(knob)
    if on:
        assert at > WIDTH // 2, drawn
    else:
        assert at < WIDTH // 2, drawn


@pytest.mark.parametrize("ends", [True, False])
def test_the_ends_are_round_only_where_drawn(ends: bool) -> None:
    """P2."""
    drawn = pill(True, "ON", ends=ends, **_PAINT).plain
    assert (drawn[0], drawn[-1]) == (ROUND if ends else SQUARE), drawn


@pytest.mark.parametrize(("on", "word"), _STATES)
def test_the_word_is_whole_inside_and_off_the_knob(on: bool | None, word: str) -> None:
    """P3."""
    drawn = pill(on, word, ends=True, **_PAINT).plain
    inside = drawn[1:-1]
    assert word in inside, drawn
    start = inside.index(word)
    if on is True:
        assert start + len(word) <= inside.index(KNOBS[True]), drawn
    if on is False:
        assert start > inside.index(KNOBS[True]), drawn


@pytest.mark.parametrize(("on", "word"), _STATES)
def test_every_state_is_one_line_of_the_switch_width(
    on: bool | None, word: str
) -> None:
    """P4."""
    drawn = pill(on, word, ends=False, **_PAINT).plain
    assert "\n" not in drawn, drawn
    assert len(drawn) == WIDTH, (len(drawn), drawn)


def test_each_cell_takes_its_colour() -> None:
    """P5."""
    text = pill(True, "ON", ends=True, **_PAINT)
    drawn = text.plain

    def style(at: int):
        return text.get_style_at_offset(Console(), at)

    def hex_of(colour: RichColor | None) -> str | None:
        return None if colour is None else colour.get_truecolor().hex.lower()

    for end in (0, WIDTH - 1):  # the round ends: track colour on the panel
        assert hex_of(style(end).color) == _PAINT["track"], style(end)
        assert style(end).bgcolor is None, style(end)
    blank = drawn.index(" ")
    assert hex_of(style(blank).bgcolor) == _PAINT["track"], style(blank)
    knob = style(drawn.index(KNOBS[True]))
    assert hex_of(knob.color) == _PAINT["knob"], knob
    assert hex_of(knob.bgcolor) == _PAINT["track"], knob
    word = style(drawn.index("ON"))
    assert hex_of(word.color) == _PAINT["ink"], word
    assert hex_of(word.bgcolor) == _PAINT["track"], word
    assert word.bold, word


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({"TERM_PROGRAM": "ghostty"}, True),
        ({"TERM_PROGRAM": "WezTerm"}, True),
        ({"TERM": "xterm-kitty"}, True),
        ({"TERM_PROGRAM": "Apple_Terminal"}, False),
        ({}, False),
        ({"TERM_PROGRAM": "Apple_Terminal", "ONEX_TUI_ROUND_ENDS": "1"}, True),
        ({"TERM_PROGRAM": "ghostty", "ONEX_TUI_ROUND_ENDS": "0"}, False),
    ],
)
def test_round_ends_follow_the_terminal(
    environ: dict[str, str], expected: bool
) -> None:
    """P6."""
    assert round_ends(environ) is expected


@pytest.mark.parametrize("ends", [True, False])
def test_the_knob_suits_the_terminal(ends: bool) -> None:
    """P7."""
    assert KNOBS == {True: "\uf111", False: "●"}
    for on in (True, False):
        drawn = pill(on, "ON" if on else "OFF", ends=ends, **_PAINT).plain
        assert KNOBS[ends] in drawn, drawn
        assert KNOBS[not ends] not in drawn, drawn
