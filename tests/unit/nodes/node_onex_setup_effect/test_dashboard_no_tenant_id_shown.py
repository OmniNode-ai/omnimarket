# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC4 (OMN-26226): no tenant id shown; unpriced runs are not measured."""

from __future__ import annotations

import json

import pytest

from tests.unit.nodes.node_onex_setup_effect import test_onex_tui_dashboard as dash
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import (
    _app,
    _log_text,
    _Runner,
)

pytestmark = pytest.mark.unit

_PLANTED_TENANT = "planted-tenant-uuid-1234"
_UNPRICED = json.dumps(
    {
        "status": "success",
        "run_id": "ebba3940-0000-0000-0000-000000000000",
        "result": {
            "status": "completed",
            "model_name": "Beam-1.0-501B-A23B",
            "response": "A receipt proves the run happened.",
            "metrics": {
                "total_tokens": 123,
                "latency_ms": 1000,
                "cost_usd": 0.0,
                "cost_measured": False,
                "cost_savings_usd": 0.0015,
            },
        },
    }
)


async def test_a_planted_tenant_id_is_not_shown_on_the_dashboard_screen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        dash,
        "_RUNNING",
        json.dumps(
            {
                "step": "dashboard",
                "status": "success",
                "changed": False,
                "evidence": {
                    "url": "http://127.0.0.1:52463",
                    "pid": "35653",
                    "tenant_id": _PLANTED_TENANT,
                },
                "fix": None,
            }
        ),
    )
    app = dash._app(dash._Dashboard(running=True))
    async with app.run_test(size=(160, 44)) as pilot:
        await dash._open_dashboard(pilot)
        text = dash._state(app)
        assert _PLANTED_TENANT not in text


async def test_an_unpriced_run_shows_cost_and_savings_as_not_measured() -> None:
    runner = _Runner([], captured="diagnostic line\n" + _UNPRICED + "\n")
    app = _app(runner)
    async with app.run_test() as pilot:
        for key in "hello":
            await pilot.press(key)
        await pilot.press("enter")
        await pilot.pause()
        text = _log_text(app)
        assert "Beam-1.0-501B-A23B" in text
        # The run line wraps in the output, so read it as one rendered string
        # (the OMN-26620 output is a stack of blocks, not a log of segments).
        flat = " ".join(text.split())
        assert "not recorded" in flat
        assert "savings not measured" in flat
        assert "$0.0000" not in text
        assert "saved" not in text.lower()
        status = str(app.query_one("#status").render())
        assert "$0.0000" not in status
