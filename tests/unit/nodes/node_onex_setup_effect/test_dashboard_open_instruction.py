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
)

pytestmark = pytest.mark.unit

_URL = "http://127.0.0.1:52463"


async def test_the_dashboard_screen_instructs_how_to_open() -> None:
    app = _app(_Dashboard(running=True))
    async with app.run_test(size=(160, 44)) as pilot:
        await _open_dashboard(pilot)
        assert isinstance(app.screen, DashboardScreen)
        # OMN-26620 (mockup page 7): the address shows under the title and the
        # one instruction is the footer line; the token is never on screen.
        from tests.unit.nodes.node_onex_setup_effect.tabs_redesign_support import (
            screen_text,
        )

        text = screen_text(app)
        assert "enter open" in text
        assert "Open in browser" in text
        assert _URL in text
        assert "token" not in text.lower()


async def test_a_start_shows_how_to_open_instead_of_the_address() -> None:
    runner = _Dashboard(running=False)
    app = _app(runner)
    async with app.run_test(size=(160, 44)) as pilot:
        await _open_dashboard(pilot)
        await pilot.press("s")
        await _settle(pilot, 16)
        from tests.unit.nodes.node_onex_setup_effect.tabs_redesign_support import (
            screen_text,
        )

        text = screen_text(app)
        assert "enter open" in text  # the footer is the one instruction
        assert "Press Enter" not in text


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


def test_the_foreground_serve_never_offers_the_bare_address_to_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    """The foreground ``onex dashboard`` serve path, handler stubbed.

    Failure modes: the address printed alone on a line, or with no word that it
    needs this start's token; the line that setup reads the port from no longer
    starting with the address.
    """
    import types
    from urllib.parse import urlsplit
    from uuid import UUID

    from click.testing import CliRunner

    import omnimarket.cli.cli_local as cli_local

    class _StubHandler:
        def __init__(self, pages: object) -> None:
            self.pages = pages

        async def handle(self, request: object) -> None:
            return None

    monkeypatch.setattr(
        cli_local, "resolve_dashboard_bind", lambda _overlay: ("127.0.0.1", 18080)
    )
    monkeypatch.setattr(cli_local, "ensure_bundle", lambda: tmp_path)
    monkeypatch.setattr(
        cli_local,
        "read_local_tenant_identity",
        lambda: types.SimpleNamespace(
            tenant_uuid=UUID("5f0e1d2c-3b4a-4c5d-8e6f-7a8b9c0d1e2f")
        ),
    )
    monkeypatch.setattr(cli_local, "HandlerLocalDashboardServe", _StubHandler)
    result = CliRunner().invoke(cli_local.dashboard_command, [])
    assert result.exit_code == 0, result.output
    address = "http://127.0.0.1:18080/"
    lines = [line for line in result.output.splitlines() if address in line]
    assert lines, "the serve path no longer prints its address"
    for line in lines:
        assert line.strip() not in {address, f"onex dashboard: {address}"}, (
            "the bare address is printed on its own"
        )
        assert "needs this start's token" in line, (
            "the address is offered without saying it needs this start's token"
        )
        assert "onex dashboard open" in line, "the line does not say how to open it"
    first = lines[0]
    assert first.startswith("onex dashboard: "), "setup can no longer find the line"
    start = urlsplit(first[len("onex dashboard: ") :].split(" ", 1)[0])
    assert (start.hostname, start.port) == ("127.0.0.1", 18080)
