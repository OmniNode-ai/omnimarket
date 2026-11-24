# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1 (OMN-26226): the dashboard screen and onex dashboard output say how to open."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_onex_setup_effect.cli_tui import (
    DashboardScreen,
)
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui_dashboard import (
    _app,
    _Dashboard,
    _open_dashboard,
    _settle,
    _state,
)

pytestmark = pytest.mark.unit

_URL = "http://127.0.0.1:52463"


async def test_the_dashboard_screen_instructs_how_to_open() -> None:
    app = _app(_Dashboard(running=True))
    async with app.run_test(size=(160, 44)) as pilot:
        await _open_dashboard(pilot)
        assert isinstance(app.screen, DashboardScreen)
        text = _state(app)
        assert "Enter" in text
        assert "Open" in text
        assert "onex dashboard open" in text
        assert _URL not in text


async def test_a_start_shows_how_to_open_instead_of_the_address() -> None:
    runner = _Dashboard(running=False)
    app = _app(runner)
    async with app.run_test(size=(160, 44)) as pilot:
        await _open_dashboard(pilot)
        await pilot.press("s")
        await _settle(pilot, 16)
        note = str(app.screen.query_one("#bnote").render())
        assert "Enter" in note
        assert "Open" in note
        assert "onex dashboard open" in note
        assert _URL not in note


def test_onex_dashboard_output_instructs_how_to_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnimarket.nodes.node_onex_setup_effect import cli_dashboard as cd
    from omnimarket.nodes.node_onex_setup_effect.handlers import handler_setup_dashboard
    from omnimarket.nodes.node_onex_setup_effect.models.model_setup_observation import (
        ModelSetupObservation,
    )

    class _Running:
        def observe(self, plan):
            return ModelSetupObservation(
                step="dashboard",
                status="success",
                evidence={"url": _URL, "pid": "35653"},
                fix=None,
            )

    monkeypatch.setattr(handler_setup_dashboard, "HandlerSetupDashboard", _Running)
    from click.testing import CliRunner

    result = CliRunner().invoke(cd.dashboard_status, [])
    assert result.exit_code == 0, result.output
    assert "onex dashboard open" in result.output
    assert _URL not in result.output
