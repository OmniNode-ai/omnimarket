"""OMN-26620: /metering and /theme are full screens like /config, outside the four tabs.

/metering shows four colourful cards (tasks, tokens, cost, saved) and a by-model box from the
real `onex metering` answer; an unmeasured cost reads "not measured", never 0; no usage and a
failed read each say so in plain words. /theme lists every theme, previews the highlighted one
on the whole screen with its colour swatches, Enter keeps it and Esc puts the old one back.
Each test was red on the previous head: neither screen existed.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from omnimarket.nodes.node_onex_setup_effect.cli_tui import ModelTuiStatus, OnexTui
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import tui_design

_REPORT = {
    "runs_total": 300,
    "spend_usd": "0.50",
    "savings_usd": "3.00",
    "tokens_in": 9000,
    "tokens_out": 1200,
    "by_model": [
        {
            "model": "Qwen3.8-27B",
            "runs": 200,
            "tokens_in": 6000,
            "tokens_out": 800,
            "spend_usd": "0.30",
        },
        {"model": "Beam-1.0", "runs": 100, "tokens_in": 3000, "tokens_out": 400},
    ],
}


class _Meter:
    """A runner whose metering answer is chosen per test."""

    def __init__(self, report: object, code: int = 0) -> None:
        self._out = json.dumps(report) if report is not None else ""
        self._code = code

    async def capture(
        self, argv: tuple[str, ...], stdin: str | None = None
    ) -> tuple[int, str, str]:
        return self._code, self._out, ""

    async def stream(
        self,
        argv: tuple[str, ...],
        emit: Callable[[str], None],
        stdin: str | None = None,
    ) -> int:
        emit(self._out)
        return self._code


async def _ready() -> ModelTuiStatus:
    return ModelTuiStatus(model="Qwen3.8-27B", delegation="off", models=2)


async def _open(
    command: str, runner: object, size: tuple[int, int] = (140, 40)
) -> tuple[str, str]:
    app = OnexTui(load_status=_ready, runner=runner)
    async with app.run_test(size=size) as pilot:
        for _ in range(3):
            await pilot.pause()
        await pilot.press(*command, "enter")
        for _ in range(12):
            await pilot.pause(0.1)
        text = "\n".join(
            "".join(segment.text for segment in strip)
            for strip in app.screen._compositor.render_strips()
        )
        return type(app.screen).__name__, text


async def test_metering_opens_a_full_screen_with_four_cards_and_a_by_model_box() -> (
    None
):
    kind, text = await _open("/metering", _Meter(_REPORT))
    assert kind == "MeteringScreen"
    for word in ("Usage and cost", "Tasks", "Tokens", "Cost", "Saved"):
        assert word in text, word
    assert "300" in text
    assert "$0.50" in text
    assert "$3.00" in text
    assert "modelled" in text
    assert "By model" in text
    assert "Qwen3.8-27B" in text
    assert "Savings are modelled, not billed." in text


@pytest.mark.parametrize("command", ["/metering", "/theme"])
async def test_metering_and_theme_are_not_tabs(command: str) -> None:
    """The tab bar stays Config, Model, Delegation, Dashboard; Tab does not leave."""
    _, text = await _open(command, _Meter(_REPORT))
    assert "Delegation" not in text
    assert [spec.id for spec in tui_design().tabs] == [
        "config",
        "model",
        "delegation",
        "dashboard",
    ]


async def test_an_unmeasured_cost_never_reads_as_zero() -> None:
    report = {**_REPORT, "spend_usd": None, "savings_usd": None}
    _, text = await _open("/metering", _Meter(report))
    assert "not measured" in text
    assert "$0.00" not in text


async def test_no_usage_says_what_will_appear() -> None:
    _, text = await _open("/metering", _Meter({}))
    assert "first delegated task" in text


async def test_a_failed_read_is_plain_and_shows_no_raw_text() -> None:
    _, text = await _open("/metering", _Meter(None, code=1))
    assert "Could not read your usage" in text
    assert "Traceback" not in text
    assert "Errno" not in text


async def test_theme_opens_a_full_screen_that_lists_every_theme_with_swatches() -> None:
    kind, text = await _open("/theme", _Meter(_REPORT))
    assert kind == "ThemeScreen"
    for name in ("onex-dark", "onex-light", "nord"):
        assert name in text, name
    assert tui_design().words["themes_preview_title"] in text
    assert "██" in text, "the colour swatches are missing"


async def test_highlighting_a_theme_previews_it_and_escape_puts_the_old_one_back() -> (
    None
):
    app = OnexTui(load_status=_ready, runner=_Meter(_REPORT))
    async with app.run_test(size=(140, 40)) as pilot:
        for _ in range(3):
            await pilot.pause()
        before = str(app.theme)
        await pilot.press(*"/theme", "enter")
        for _ in range(8):
            await pilot.pause(0.1)
        await pilot.press("down")
        for _ in range(3):
            await pilot.pause()
        assert str(app.theme) != before, "the highlighted theme is not previewed"
        await pilot.press("escape")
        for _ in range(4):
            await pilot.pause()
        assert str(app.theme) == before
