# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26620 independent-review defects D1 to D4, each driven through the real
screen a person sees.

Failure modes:
  D1  (AC3) a delegated answer shows no task card; a record with no cost reads
      "$None".
  D2  (AC4) the Config tab draws a footer that is not footer.screens.config,
      so the footer test checks data the screen never shows.
  D3  a /doctor row that needs attention shows no reason.
  D4  a fresh home shows "reading your setup" for ever when the read fails or
      never returns.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from omnimarket.nodes.node_onex_setup_effect import cli_tui
from omnimarket.nodes.node_onex_setup_effect.cli_tui import (
    ModelTuiStatus,
    OnexTui,
    StatusStore,
    TaskCard,
)
from omnimarket.nodes.node_onex_setup_effect.doctor_page import render_doctor_page
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import tui_design
from tests.unit.nodes.node_onex_setup_effect import tabs_redesign_support as sup
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import _root, _Runner

pytestmark = pytest.mark.unit


def _answer(metrics: dict[str, object]) -> str:
    return json.dumps(
        {
            "status": "success",
            "run_id": "175dde92-4151-4d52-ac68-aca5e4042afd",
            "result": {
                "status": "completed",
                "model_name": "Qwen3.8-27B",
                "response": "A receipt proves the run happened.",
                "metrics": metrics,
            },
        }
    )


async def _submit(captured: str, size: tuple[int, int] = (140, 40)) -> str:
    runner = _Runner([], captured=captured + "\n")
    app = sup.app(runner)
    async with app.run_test(size=size) as pilot:
        for key in "hello":
            await pilot.press(key)
        await pilot.press("enter")
        await sup.settle(pilot)
        return sup.screen_text(app)


# ---------------------------------------------------------------- D1 (AC3)


async def test_d1_a_recorded_answer_is_drawn_as_a_task_card() -> None:
    shown = await _submit(
        _answer(
            {
                "input_tokens": 1200,
                "output_tokens": 340,
                "latency_ms": 2400,
                "cost_usd": 0.0042,
            }
        )
    )
    card = tui_design().task_card
    assert f"{card.glyphs['done']} {card.labels['done']}" in shown, shown
    for fact in ("Qwen3.8-27B", "1.5k tokens", "2.4 s", "$0.0042"):
        assert fact in shown, (fact, shown)
    assert tui_design().drawing.card_bar in shown, shown


async def test_d1_a_record_without_a_cost_says_not_recorded() -> None:
    shown = await _submit(_answer({"total_tokens": 2257, "latency_ms": 6724}))
    assert "None" not in shown, shown
    assert "not recorded" in shown, shown


def test_d1_a_card_missing_every_figure_never_says_none() -> None:
    line = TaskCard.from_recorded({"model": "Qwen3.8-27B"}).facts_line()
    assert "None" not in line, line
    assert line.count("not recorded") == 3, line


# ---------------------------------------------------------------- D2 (AC4)


@pytest.mark.parametrize("width", [80, 140])
async def test_d2_the_config_tab_draws_footer_screens_config(width: int) -> None:
    the_app = sup.app()
    async with the_app.run_test(size=(width, 40)) as pilot:
        await sup.open_tab(pilot, the_app, "config")
        keys = the_app.screen.query_one("#keys")
        drawn = str(keys.render())
        assert keys.region.height == 1, (width, keys.region)
        assert "\n" not in drawn, drawn
        assert "…" not in drawn, drawn
        for key in tui_design().footer.screens["config"].keys:
            assert f"{key.label} {key.words}".strip() in drawn, (key, drawn)


# ---------------------------------------------------------------- D3


def test_d3_a_row_that_needs_attention_gives_its_reason() -> None:
    # The wording `onex doctor --json` gives on a fresh home (seen 2026-11-27).
    payload = {
        "total": 1,
        "passed": 0,
        "failed": 1,
        "checks": [
            {
                "name": "Delegation local model",
                "status": "unhealthy",
                "message": (
                    "No model is set up, local or hosted. Fix: Run `onex` and "
                    "open /config to add a provider or a Local Model."
                ),
            }
        ],
    }
    row = next(
        line
        for line in render_doctor_page(payload, width=140)
        if "Delegation local model" in line
    )
    assert "No model is set up, local or hosted." in row, row
    assert "fix: /config" in row, row
    assert "`" not in row, row


# ---------------------------------------------------------------- D4


def _bar(app: OnexTui) -> str:
    return " ".join(str(app.query_one("#status").render()).split())


async def _failing() -> ModelTuiStatus:
    raise RuntimeError("models list could not be read")


async def _never() -> ModelTuiStatus:
    await asyncio.Event().wait()
    raise AssertionError("unreachable")


@pytest.mark.parametrize("reader", [_failing, _never])
async def test_d4_a_fresh_home_never_reads_for_ever(
    reader: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_tui, "_status_timeout", lambda: 0.05)
    design = tui_design()
    app = OnexTui(root=_root, runner=_Runner([]), load_status=reader)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await asyncio.sleep(0.3)
        await sup.settle(pilot)
        bar = _bar(app)
        assert design.words["status_reading"] not in bar, bar
        assert "could not read your setup" in bar, bar


async def test_d4_a_saved_status_never_reads_even_when_the_read_hangs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_tui, "_status_timeout", lambda: 0.05)
    StatusStore.save(ModelTuiStatus(model="Saved-Model", delegation="off", models=1))
    app = OnexTui(root=_root, runner=_Runner([]), load_status=_never)
    async with app.run_test() as pilot:
        for _ in range(2):
            await pilot.pause()
            assert tui_design().words["status_reading"] not in _bar(app)
        await asyncio.sleep(0.3)
        await sup.settle(pilot)
        bar = _bar(app)
        assert tui_design().words["status_reading"] not in bar, bar
        assert "Saved-Model" in bar, bar
