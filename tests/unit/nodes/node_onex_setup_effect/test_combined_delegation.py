# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The delegation switches must never select a different Claude model or connection.

Failure controls: a remaining mode chooser offers a gateway; Save connects a
model unnecessarily.
"""

import pytest
from click.testing import CliRunner
from textual.widgets import RadioButton, Switch

from omnimarket.nodes.node_onex_setup_effect import cli_delegation
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import (
    _app,
    _Runner,
    _selected_rows,
    _type,
)

pytestmark = pytest.mark.unit


def test_cli_has_no_gateway_mode() -> None:
    result = CliRunner().invoke(cli_delegation.delegation_group, ["on", "--help"])
    assert result.exit_code == 0
    assert "--mode" not in result.output
    assert "--gateway-model" not in result.output


async def test_one_switch_uses_only_hook_commands_for_any_selected_model() -> None:
    runner = _Runner([], by_command=_selected_rows(connection=True))
    app = _app(runner)
    async with app.run_test(size=(140, 44)) as pilot:
        await _type(pilot, "/delegation")
        await pilot.press("enter")
        for _ in range(6):
            await pilot.pause()
        assert not app.screen.query(RadioButton)
        # One hook, one switch (OMN-23413 Amendment 14a), and nothing else to choose.
        assert len(app.screen.query(Switch)) == 1
        app.screen.set_all(True)
        await pilot.press("ctrl+s")  # no Save button (OMN-26688)
        for _ in range(6):
            await pilot.pause()
        assert ("delegation", "on", "--json") in runner.calls
        assert not any("--gateway-model" in call for call in runner.calls)
        assert not any(call[:2] == ("models", "connect") for call in runner.calls)
