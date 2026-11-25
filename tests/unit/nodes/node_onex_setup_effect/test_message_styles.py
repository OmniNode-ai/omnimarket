"""OMN-26620: each kind of message has its own colour, and the welcome steps back once you start.

Operator feedback on real screens: the working line, the task echo, the answer facts and the
cancelled line were all one yellow, and the diagram and charts stayed on screen after a task
started. Each test was red on the previous head.
"""

from __future__ import annotations

from omnimarket.nodes.node_onex_setup_effect.cli_tui import (
    ModelTuiStatus,
    OnexTui,
    activity_renderable,
    facts_renderable,
    task_echo_renderable,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import tui_design

from .test_setup_guide import _Quiet


def _style_at(text: object, index: int) -> str:
    for span in text.spans:  # type: ignore[attr-defined]
        if span.start <= index < span.end:
            return str(span.style).lower()
    return ""


def test_the_working_line_colours_the_spinner_the_verb_and_the_hint_differently() -> (
    None
):
    theme = tui_design().theme
    text = activity_renderable("⠋", "Thinking", 5)
    plain = text.plain
    spinner = _style_at(text, 0)
    verb = _style_at(text, plain.index("Thinking"))
    hint = _style_at(text, plain.index("esc to cancel"))
    assert theme.primary.lower() in spinner
    assert theme.foreground.lower() in verb
    assert theme.muted.lower() in hint
    assert len({spinner, verb, hint}) == 3


def test_the_task_echo_marks_the_prompt_in_gold_and_keeps_the_task_in_white() -> None:
    theme = tui_design().theme
    text = task_echo_renderable("what can you do?")
    assert text.plain.endswith("what can you do?")
    assert theme.primary.lower() in _style_at(text, 0)
    assert theme.foreground.lower() in _style_at(text, text.plain.index("what"))


def test_the_answer_facts_give_the_model_savings_and_the_rest_their_own_colours() -> (
    None
):
    theme = tui_design().theme
    text = facts_renderable(
        [
            ("Qwen3.8-27B", "model"),
            ("↓312 in · ↑11 out", "detail"),
            ("$0.0000", "cost"),
            ("saved $0.0007", "saved"),
        ],
        "run 6c47700f",
    )
    plain = text.plain
    assert plain.startswith("Qwen3.8-27B  ·  ↓312 in")
    assert theme.accent.lower() in _style_at(text, 0)
    assert theme.success.lower() in _style_at(text, plain.index("saved"))
    assert theme.muted.lower() in _style_at(text, plain.index("↓312"))
    assert plain.endswith("run 6c47700f")


async def _ready() -> ModelTuiStatus:
    return ModelTuiStatus(model="Qwen3.8-27B", delegation="off", models=3)


async def _screen(app: OnexTui, size: tuple[int, int], keys: tuple[str, ...]) -> str:
    async with app.run_test(size=size) as pilot:
        for _ in range(3):
            await pilot.pause()
        if keys:
            await pilot.press(*keys)
            for _ in range(4):
                await pilot.pause()
        return "\n".join(
            "".join(segment.text for segment in strip)
            for strip in app.screen._compositor.render_strips()
        )


async def test_the_diagram_goes_away_once_you_start() -> None:
    before = await _screen(OnexTui(load_status=_ready), (140, 52), ())
    assert "Smart Routing" in before
    # a task ends the welcome; a /command keeps it (test_welcome_stays_for_commands)
    after = await _screen(
        OnexTui(load_status=_ready, runner=_Quiet()),  # type: ignore[arg-type]
        (140, 52),
        (*"hello", "enter"),
    )
    assert "Smart Routing" not in after
    assert "Build more. Spend less." in after, "the logo and headline stay"
