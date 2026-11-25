"""OMN-26620: the TUI uses the terminal's own background (operator decision).

Failure modes this guards, each with a demonstrated failing case on the head
before the change:
  1. the screen, input bar or footer paints its own fill (the screen was a forced
     #000000 and the footer a tinted #0a0a0a band);
  2. the yaml switch cannot bring the forced black back;
  3. text colours fall below a readable contrast on a black or a dark-grey terminal.
"""

from __future__ import annotations

from omnimarket.nodes.node_onex_setup_effect.cli_tui import ModelTuiStatus, OnexTui
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import tui_design

_FILLS_THE_TERMINAL_SHOWS_THROUGH = {"default", "color(7)", "None"}


async def _ready() -> ModelTuiStatus:
    return ModelTuiStatus(model="Qwen3.8-27B", delegation=True)


def _backgrounds(app: OnexTui) -> set[str]:
    fills: set[str] = set()
    for strip in app.screen._compositor.render_strips():
        for segment in strip:
            style = segment.style
            fills.add(str(style.bgcolor.name) if style and style.bgcolor else "None")
    return fills


async def test_the_screen_paints_no_fill_of_its_own() -> None:
    """The start screen, the input bar and the footer all show the terminal."""
    app = OnexTui(load_status=_ready)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        fills = _backgrounds(app)
    painted = {fill for fill in fills if fill not in _FILLS_THE_TERMINAL_SHOWS_THROUGH}
    assert not painted, f"the TUI paints fills of its own: {sorted(painted)}"


def test_the_yaml_switch_brings_the_forced_black_back() -> None:
    theme = tui_design().theme
    natural = theme.paint(ground="terminal")
    black = theme.paint(ground="black")
    assert natural.ansi is True
    assert black.ansi is False
    assert black.background == "#000000"
    assert black.surface == "#000000"
    assert black.panel == "#0a0a0a"


def test_the_default_ground_is_the_terminal() -> None:
    assert tui_design().theme.ground == "terminal"


def _luminance(colour: str) -> float:
    channels = [int(colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [
        c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels
    ]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _contrast(foreground: str, background: str) -> float:
    a, b = sorted((_luminance(foreground), _luminance(background)), reverse=True)
    return (a + 0.05) / (b + 0.05)


def test_text_and_borders_stay_readable_on_black_and_on_dark_grey() -> None:
    """The terminal decides the background, so the colours must work on both."""
    theme = tui_design().theme
    checks: list[tuple[str, str, float]] = [
        (theme.foreground, "text", 4.5),
        (theme.muted, "muted text", 4.5),
        (theme.primary, "brand text", 4.5),
        (theme.border, "border", 3.0),
    ]
    for terminal in ("#000000", "#1e1e1e"):
        for colour, name, needed in checks:
            got = _contrast(colour, terminal)
            assert got >= needed, (
                f"{name} {colour} on {terminal} is {got:.2f}, need {needed}"
            )
