"""OMN-26620: the /metering by-model table never wraps a row.

Failure mode (100x34, 2026-11-25): the table was fixed-width text, so in a narrower
terminal each row spilled its cost onto a second line ("not" / "measured").
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_onex_setup_effect.cli_tui import ModelTuiStatus, OnexTui

from .test_metering_theme_screens import _Meter

_REPORT = {
    "runs_total": 4350,
    "spend_usd": "0.00",
    "savings_usd": "36.01",
    "tokens_in": 8000000,
    "tokens_out": 200000,
    "by_model": [
        {
            "model": "Qwen3.8-27B",
            "runs": 4310,
            "tokens_in": 7800000,
            "tokens_out": 200000,
            "spend_usd": "0.00",
        },
        {
            "model": "Beam-1.0-501B-A23B",
            "runs": 39,
            "tokens_in": 170000,
            "tokens_out": 6600,
        },
        {
            "model": "cohere/north-mini-code:free",
            "runs": 1,
            "tokens_in": 0,
            "tokens_out": 0,
        },
    ],
}


async def _ready() -> ModelTuiStatus:
    return ModelTuiStatus(model="Qwen3.8-27B", delegation="off", models=5)


@pytest.mark.parametrize("size", [(140, 46), (100, 34), (80, 30)])
async def test_each_model_is_one_row_with_its_cost(size: tuple[int, int]) -> None:
    app = OnexTui(runner=_Meter(_REPORT), load_status=_ready)
    async with app.run_test(size=size) as pilot:
        for key in "/metering":
            await pilot.press("slash" if key == "/" else key)
        await pilot.press("enter")
        for _ in range(8):
            await pilot.pause()
        box = app.screen.query_one("#mmodels")
        lines = [
            "".join(segment.text for segment in strip)
            for strip in app.screen._compositor.render_strips()
        ]
        rows = [
            lines[y][box.region.x : box.region.right]
            for y in range(box.region.y + 1, box.region.bottom - 1)
        ]
        body = [r for r in rows if r.strip(" │")]
        # the heading and one row per model, nothing spilled onto a second line
        assert len(body) == 1 + 3, body
        assert any("Qwen3.8-27B" in r and "$0.00" in r for r in body), body
        assert any("Beam" in r and "not measured" in r for r in body), body
