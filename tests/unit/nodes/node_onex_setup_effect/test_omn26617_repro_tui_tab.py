"""OMN-26617 AC3 reproduction (adapted)."""

import json

import pytest
from textual.widgets import Label, Static

from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import (
    _app,
    _Runner,
    _selected_rows,
    _type,
)

pytestmark = pytest.mark.unit

_NEXT_STEP = "Run onex delegation on, or switch it on in onex under /delegation."


def _status_document() -> str:
    return json.dumps(
        {
            "status": "off",
            "claude_code": "ready",
            "switches": {"prompts": "on"},
            "fix": _NEXT_STEP,
            "observations": [
                {"step": "claude-plugin", "status": "success"},
                {
                    "step": "claude-hooks",
                    "status": "action_required",
                    "fix": (
                        "Install or repair the packaged delegation hook assets "
                        "with onex install omninode-claude and check the "
                        "selected Claude settings, then retry setup."
                    ),
                },
            ],
        }
    )


@pytest.mark.asyncio
async def test_delegation_tab_shows_the_cli_next_step_when_off_and_ready() -> None:
    rows = {**_selected_rows(connection=False)}
    rows[("delegation", "status")] = _status_document()
    app = _app(_Runner([], by_command=rows))
    async with app.run_test(size=(140, 44)) as pilot:
        await _type(pilot, "/delegation")
        await pilot.press("enter")
        for _ in range(12):
            await pilot.pause()
        shown = (
            " ".join(
                " ".join(str(widget.render()).split())
                for widget in app.screen.query(Static)
            )
            + " "
            + " ".join(
                " ".join(str(widget.render()).split())
                for widget in app.screen.query(Label)
            )
        )
        assert "onex delegation on" in shown
        assert "onex install" not in shown
