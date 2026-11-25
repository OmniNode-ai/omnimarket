"""OMN-26620: the Setup Guide is a static welcome guide.

Four boxes (Config, Model, Delegation, Dashboard) each say what the feature does. They show
no live status: those tabs already report it. A click or the keys 1 to 4 on an empty prompt
open the matching tab. The guide comes first, the diagram below it under its own heading, and
both go away once something is submitted.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from omnimarket.nodes.node_onex_setup_effect.cli_tui import ModelTuiStatus, OnexTui
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import tui_design


class _Quiet:
    """A runner that answers every command at once with an empty success."""

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


async def _ready() -> ModelTuiStatus:
    return ModelTuiStatus(model="Qwen3.8-27B", delegation="off", models=5)


async def _screen(
    app: OnexTui, size: tuple[int, int], keys: tuple[str, ...] = ()
) -> str:
    async with app.run_test(size=size) as pilot:
        for _ in range(4):
            await pilot.pause()
        if keys:
            await pilot.press(*keys)
            for _ in range(4):
                await pilot.pause()
        return "\n".join(
            "".join(segment.text for segment in strip)
            for strip in app.screen._compositor.render_strips()
        )


def _words(text: str) -> str:
    return " ".join(text.split())


async def test_each_box_says_what_its_feature_does() -> None:
    guide = tui_design().welcome.guide
    text = _words(await _screen(OnexTui(load_status=_ready), (140, 46)))
    assert guide.title in text
    for step in guide.steps:
        assert step.title in text, step.title
        # a description wraps inside its box, so look for its opening words
        assert " ".join(step.text.split()[:2]) in text, step.text


async def test_the_guide_shows_no_live_status() -> None:
    """The tabs report status; the guide only explains."""
    welcome = tui_design().welcome
    text = await _screen(OnexTui(load_status=_ready), (140, 46))
    guide_area = text[
        text.index(welcome.guide.title) : text.index(welcome.diagram.heading)
    ]
    for live in (
        "models ready",
        "Qwen3.8-27B",
        "Next:",
        "reading",
        "Unknown",
        "Off",
        "Press 1",
    ):
        assert live not in guide_area, live


async def test_the_guide_comes_first_then_the_diagram_under_its_heading() -> None:
    welcome = tui_design().welcome
    text = await _screen(OnexTui(load_status=_ready), (140, 46))
    assert text.index(welcome.guide.title) < text.index(welcome.diagram.heading)
    assert text.index(welcome.diagram.heading) < text.index("Smart Routing")


async def test_a_shorter_terminal_has_the_diagram_but_no_guide() -> None:
    text = await _screen(OnexTui(load_status=_ready), (140, 38))
    assert "Smart Routing" in text
    assert tui_design().welcome.guide.title not in text


async def test_clicking_a_box_opens_its_tab() -> None:
    app = OnexTui(load_status=_ready, runner=_Quiet())  # type: ignore[arg-type]
    async with app.run_test(size=(140, 46)) as pilot:
        for _ in range(4):
            await pilot.pause()
        await pilot.click("#guide-model")
        for _ in range(8):
            await pilot.pause(0.1)
        assert type(app.screen).__name__ == "ModelPickScreen"


async def test_a_number_on_an_empty_prompt_opens_the_step_and_in_a_task_it_is_text() -> (
    None
):
    app = OnexTui(load_status=_ready)
    async with app.run_test(size=(140, 46)) as pilot:
        for _ in range(4):
            await pilot.pause()
        await pilot.press("3")
        for _ in range(4):
            await pilot.pause()
        assert type(app.screen).__name__ == "DelegationScreen"
    typed = OnexTui(load_status=_ready)
    async with typed.run_test(size=(140, 46)) as pilot:
        for _ in range(4):
            await pilot.pause()
        await pilot.press("a", "3")
        assert typed.query_one("#prompt").value == "a3"  # type: ignore[attr-defined]
        assert type(typed.screen).__name__ == "Screen"


async def test_the_guide_and_the_diagram_go_away_once_you_start() -> None:
    welcome = tui_design().welcome
    text = await _screen(OnexTui(load_status=_ready), (140, 46), (*"/clear", "enter"))
    assert welcome.guide.title not in text
    assert welcome.diagram.heading not in text
    assert "Smart Routing" not in text


async def test_the_guide_arrows_are_still() -> None:
    app = OnexTui(load_status=_ready)
    async with app.run_test(size=(140, 46)) as pilot:
        for _ in range(4):
            await pilot.pause()
        seen = set()
        for _ in range(5):
            seen.add("".join(str(w.render()) for w in app.query(".guide-arrow")))
            await pilot.pause(0.25)
        assert len(seen) == 1, "the guide arrows change"
        assert "●" not in next(iter(seen))


async def test_the_boxes_show_no_step_numbers() -> None:
    """Operator feedback: the 1 to 4 come off the boxes; the keys still work."""
    text = await _screen(OnexTui(load_status=_ready), (140, 46))
    assert not re.search(r"\b[1-4]\s{1,3}(Config|Model|Delegation|Dashboard)", text)
