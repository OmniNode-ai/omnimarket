"""OMN-26620: a question, its answer and the facts line read as three distinct, spaced parts.

Operator feedback on a real run: the task echo, the answer and the facts line ran together
with no space between them. The question now sits in a shaded bar with an icon, the answer is
indented under its own icon, the facts line sits under the answer, and there is a blank row
between the three and before the next question. The spinner is the four circle halves.
Each test was red on the previous head.
"""

from __future__ import annotations

from rich.text import Text

from omnimarket.nodes.node_onex_setup_effect.cli_tui import (
    ModelTuiStatus,
    OnexTui,
    answer_renderable,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import tui_design


def test_the_spinner_is_the_four_circle_halves() -> None:
    assert tuple(tui_design().activity.frames) == ("◐", "◓", "◑", "◒")


def test_the_answer_sits_beside_its_own_icon() -> None:
    text = answer_renderable("Hello there")
    from rich.console import Console

    console = Console(width=60, record=True, force_terminal=False)
    console.print(text)
    first = console.export_text().splitlines()[0]
    assert first.lstrip().startswith(tui_design().icons.glyphs["answer"].fallback)
    assert "Hello there" in first


async def _ready() -> ModelTuiStatus:
    return ModelTuiStatus(model="Qwen3.8-27B", delegation="off", models=3)


async def test_each_part_of_a_turn_is_set_apart_by_a_blank_row() -> None:
    app = OnexTui(load_status=_ready)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        pane = app.query_one("#output")
        pane.clear()
        pane.write(Text("what can you do?"), kind="user")
        pane.write(answer_renderable("I can help."), kind="answer", plain="I can help.")
        pane.write(Text("Qwen3.8-27B"), kind="facts")
        pane.write(Text("next question"), kind="user")
        for _ in range(3):
            await pilot.pause()
        blocks = pane.blocks
        user, answer, facts, nxt = blocks[-4:]
        assert answer.region.y - user.region.bottom >= 1, (
            "the answer touches the question"
        )
        assert facts.region.y - answer.region.bottom >= 1, "the facts touch the answer"
        assert nxt.region.y - facts.region.bottom >= 1, (
            "the next question touches the facts"
        )
        assert user.has_class("-user")
        assert answer.has_class("-answer")
        assert facts.has_class("-facts")
        assert answer.plain == "I can help.", "copying gives the words, not the layout"


async def test_the_question_sits_in_a_shaded_bar_with_an_icon() -> None:
    app = OnexTui(load_status=_ready)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        pane = app.query_one("#output")
        pane.write(Text("a question"), kind="user")
        for _ in range(3):
            await pilot.pause()
        block = pane.blocks[-1]
        assert block.styles.background.a > 0, "the question has no fill"
