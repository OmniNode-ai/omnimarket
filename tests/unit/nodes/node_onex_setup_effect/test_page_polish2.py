"""OMN-26620: two rough edges seen in a real terminal.

* The Delegation tab showed the CLI's own long fix sentence (a shell command and
  advice for a developer) under its Claude Code line.
* The /metering table lost its columns when a model name was long or a cost read
  "not measured".
Each was red on the previous head.
"""

from __future__ import annotations

import re

from omnimarket.nodes.node_onex_setup_effect.cli_tui import ModelTuiStatus, OnexTui
from omnimarket.nodes.node_onex_setup_effect.metering_page import render_metering_page

_RAW_FIX = (
    "Install or repair the packaged delegation hook assets with onex install "
    "omninode-claude and check the selected Claude settings, then retry setup."
)


async def _ready() -> ModelTuiStatus:
    return ModelTuiStatus(model="Qwen3.8-27B", delegation="off", models=1)


async def test_the_delegation_tab_never_shows_the_cli_fix_sentence() -> None:
    app = OnexTui(load_status=_ready)

    async def claude_code() -> tuple[str, str]:
        return ("installed", _RAW_FIX)

    app.claude_code = claude_code
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press(*"/delegation", "enter")
        for _ in range(4):
            await pilot.pause()
        screen_text = "\n".join(
            "".join(segment.text for segment in strip)
            for strip in app.screen._compositor.render_strips()
        )
    assert "onex install" not in screen_text
    assert "packaged delegation hook assets" not in screen_text


def test_the_metering_table_keeps_its_columns() -> None:
    report = {
        "by_model": [
            {
                "model": "Qwen3.8-27B",
                "runs": 4310,
                "tokens_in": 8_000_000,
                "spend_usd": 0,
            },
            {"model": "Beam-1.0-501B-A23B", "runs": 39, "tokens_in": 176_000},
            {
                "model": "cohere/north-mini-code:free-with-a-very-long-suffix",
                "runs": 1,
                "tokens_in": 0,
            },
        ],
        "spend_usd": 0,
        "savings_usd": 36.01,
    }
    rows = render_metering_page(report, width=100)
    data = [row for row in rows if re.match(r"^  \S", row) and "Model" not in row]
    # only the model rows, not the totals or the closing line
    data = [row for row in data if re.search(r"\s\d+\s+\S+\s+(\$|not)", row)]
    assert len(data) == 3, rows
    edges = {re.match(r"^  .{30} +(\d+) ", row).end(1) for row in data}
    assert len(edges) == 1, f"the task counts are not in one column: {data}"
