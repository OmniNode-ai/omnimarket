"""OMN-26620: the welcome (guide and diagram) stays until a task is sent.

Operator 2026-11-25 (screenshot): "i haven't typed anything in the input box but just
used some /commands and the diagram is gone - it should only go when the user types
text and enters it, asking the delegation".
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_onex_setup_effect.cli_tui import ModelTuiStatus, OnexTui

from .test_setup_guide import _Quiet


async def _ready() -> ModelTuiStatus:
    return ModelTuiStatus(model="Qwen3.8-27B", delegation="off", models=5)


async def _screen_after(keys: tuple[str, ...]) -> str:
    app = OnexTui(load_status=_ready, runner=_Quiet())  # type: ignore[arg-type]
    async with app.run_test(size=(140, 52)) as pilot:
        for _ in range(4):
            await pilot.pause()
        await pilot.press(*keys)
        for _ in range(8):
            await pilot.pause(0.05)
        return "\n".join(
            "".join(segment.text for segment in strip)
            for strip in app.screen._compositor.render_strips()
        )


@pytest.mark.parametrize(
    "keys",
    [
        (*"/clear", "enter"),
        (*"/config", "enter", "escape"),
        (*"/metering", "enter", "escape"),
    ],
    ids=["clear", "config", "metering"],
)
async def test_a_command_keeps_the_guide_and_the_diagram(keys: tuple[str, ...]) -> None:
    text = await _screen_after(keys)
    assert "Setup Guide" in text, text[-600:]
    assert "Smart Routing" in text, text[-600:]


@pytest.mark.parametrize(
    "keys",
    [(*"what is 2 + 2?", "enter"), (*"/help", "enter"), (*"/doctor", "enter")],
    ids=["task", "help", "doctor"],
)
async def test_a_task_or_a_printed_page_ends_the_welcome(keys: tuple[str, ...]) -> None:
    """A printed page (/help, /doctor) needs the room the welcome takes."""
    text = await _screen_after(keys)
    assert "Smart Routing" not in text
    assert "Setup Guide" not in text
    assert "Build more. Spend less." in text, "the logo and headline stay"
