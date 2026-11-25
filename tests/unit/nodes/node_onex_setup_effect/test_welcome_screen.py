"""OMN-26620: the welcome screen (operator's reference image, 2026-11-25).

A roomy terminal shows a large pixel ONEX wordmark, the brand line, the tagline and
four cards (Delegate, Models, Config, History) above a rounded prompt box. A small
terminal keeps the compact header so the prompt always fits. A card is clickable.
The welcome steps aside once the reader submits something.

Each test was red on the previous head (no welcome existed).
"""

from __future__ import annotations

from collections.abc import Callable

from textual.widgets import Input

from omnimarket.nodes.node_onex_setup_effect.cli_tui import ModelTuiStatus, OnexTui


async def _ready() -> ModelTuiStatus:
    return ModelTuiStatus(model="Qwen3.8-27B", delegation="off", models=5)


class _Quiet:
    """A runner that answers every command with an empty success."""

    async def capture(
        self, argv: tuple[str, ...], stdin: str | None = None
    ) -> tuple[int, str, str]:
        return 0, "{}", ""

    async def stream(
        self,
        argv: tuple[str, ...],
        emit: Callable[[str], None],
        stdin: str | None = None,
    ) -> int:
        emit("{}")
        return 0


def _rows(app: OnexTui) -> list[str]:
    return [
        "".join(segment.text for segment in strip).rstrip()
        for strip in app.screen._compositor.render_strips()
    ]


async def _shown(width: int, height: int) -> tuple[list[str], dict[str, object]]:
    """The rows on screen and facts read while the app is still running."""
    app = OnexTui(load_status=_ready, runner=_Quiet())  # type: ignore[arg-type]
    async with app.run_test(size=(width, height)) as pilot:
        await pilot.pause()
        await pilot.pause()
        facts: dict[str, object] = {
            "welcome": app.query_one("#welcome").display,
            "prompt_bottom": app.query_one("#prompt", Input).region.bottom,
        }
        return _rows(app), facts


async def test_a_roomy_terminal_shows_the_wordmark_tagline_and_four_cards() -> None:
    rows, _ = await _shown(140, 40)
    text = "\n".join(rows)
    assert sum("█" in row for row in rows) >= 5, "the large wordmark is missing"
    assert "ONEXCLI" in text.replace(" ", ""), "the brand line is missing"
    assert "Delegate tasks to the right model, at the lowest cost." in text
    for title, line in (
        ("Delegate", "Run a task with the best"),
        ("Models", "View and manage"),
        ("Config", "Add providers, keys"),
        ("History", "See past runs, cost"),
    ):
        assert title in text, title
        assert line in text, line


async def test_a_small_terminal_keeps_the_compact_header() -> None:
    rows, facts = await _shown(80, 24)
    text = "\n".join(rows)
    assert "Delegate tasks to the right model" not in text
    assert "View and manage" not in text
    assert facts["welcome"] is False
    assert "Ask onex to delegate a task" in text, "the prompt must stay on screen"


async def test_the_prompt_stays_on_screen_at_the_roomy_and_the_smallest_size() -> None:
    for width, height in ((100, 30), (140, 40), (80, 24)):
        _, facts = await _shown(width, height)
        assert facts["prompt_bottom"] <= height, (width, height, facts)  # type: ignore[operator]


async def test_clicking_the_models_card_opens_the_model_tab() -> None:
    app = OnexTui(load_status=_ready, runner=_Quiet())  # type: ignore[arg-type]
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.click("#card-models")
        for _ in range(4):
            await pilot.pause()
        assert type(app.screen).__name__ == "ModelPickScreen"


async def test_clicking_the_delegate_card_puts_the_cursor_in_the_prompt() -> None:
    app = OnexTui(load_status=_ready, runner=_Quiet())  # type: ignore[arg-type]
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        app.query_one("#output").focus()
        await pilot.click("#card-delegate")
        await pilot.pause()
        assert app.focused is app.query_one("#prompt", Input)


async def test_the_welcome_steps_aside_once_something_is_submitted() -> None:
    app = OnexTui(load_status=_ready, runner=_Quiet())  # type: ignore[arg-type]
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        assert app.query_one("#welcome").display is True
        await pilot.press(*"/clear", "enter")
        await pilot.pause()
        await pilot.pause()
        assert app.query_one("#welcome").display is False
        assert app.query_one("#header").display is True
