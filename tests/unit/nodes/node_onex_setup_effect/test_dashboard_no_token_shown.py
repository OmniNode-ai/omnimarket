# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC3 (OMN-26226): a planted token never appears on a TUI screen or CLI output."""

from __future__ import annotations

import json

import pytest

from omnimarket.nodes.node_onex_setup_effect import cli_dashboard
from omnimarket.nodes.node_onex_setup_effect.cli_tui import DashboardScreen
from tests.unit.nodes.node_onex_setup_effect import test_onex_tui_dashboard as dash

pytestmark = pytest.mark.unit

_PLANTED = "planted-secret-token-9f8e7d6c"


async def test_a_planted_token_is_not_shown_on_the_dashboard_screen(
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
                    "url": f"http://127.0.0.1:52463/?token={_PLANTED}",
                    "pid": "35653",
                    "token": _PLANTED,
                },
                "fix": None,
            }
        ),
    )
    app = dash._app(dash._Dashboard(running=True))
    async with app.run_test(size=(160, 44)) as pilot:
        await dash._open_dashboard(pilot)
        assert isinstance(app.screen, DashboardScreen)
        text = dash._state(app)
        note = str(app.screen.query_one("#bnote").render())
        assert _PLANTED not in text
        assert _PLANTED not in note


async def test_a_planted_token_is_not_shown_on_cli_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnimarket.nodes.node_onex_setup_effect.handlers import handler_setup_dashboard
    from omnimarket.nodes.node_onex_setup_effect.models.model_setup_observation import (
        ModelSetupObservation,
    )

    class _Running:
        def observe(self, plan):
            return ModelSetupObservation(
                step="dashboard",
                status="success",
                evidence={
                    "url": f"http://127.0.0.1:52463/?token={_PLANTED}",
                    "pid": "35653",
                    "token": _PLANTED,
                },
                fix=None,
            )

    monkeypatch.setattr(handler_setup_dashboard, "HandlerSetupDashboard", _Running)
    from click.testing import CliRunner

    result = CliRunner().invoke(cli_dashboard.dashboard_status, [])
    assert result.exit_code == 0, result.output
    assert _PLANTED not in result.output
