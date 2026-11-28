# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex dashboard open`` opens the hosted dashboard, never a local server.

Each test names the failure it catches:

* the command still starts or opens a 127.0.0.1 dashboard;
* the hosted address is typed into the code instead of read from configuration;
* a user who has not signed in is sent to a page that fails, or the browser opens;
* the not-signed-in message does not name ``onex login``.
"""

from __future__ import annotations

import urllib.parse
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner
from omnibase_core.errors.model_onex_error import ModelOnexError

from omnimarket.nodes.node_onex_setup_effect import cli_dashboard

pytestmark = pytest.mark.unit

_DEV = "https://dev.dash.omninode.ai"


class _SignedIn:
    def __init__(self, *, onex_home: Path) -> None:
        pass

    def load(self) -> object:
        return object()


class _SignedOut:
    def __init__(self, *, onex_home: Path) -> None:
        pass

    def load(self) -> object:
        raise ModelOnexError(message="no stored login", error_code="E")


def _config(home: Path, url: str | None) -> None:
    target = home / ".onex"
    target.mkdir(exist_ok=True)
    body = f"dashboard:\n  url: {url}\n" if url else "{}\n"
    (target / "config.yaml").write_text(body, encoding="utf-8")


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    urls: list[str] = []
    monkeypatch.setenv("HOME", str(tmp_path))
    _config(tmp_path, _DEV)

    def launch(url: str, *args: Any, **kwargs: Any) -> int:
        urls.append(url)
        return 0

    monkeypatch.setattr(click, "launch", launch)
    monkeypatch.setattr(cli_dashboard, "StoreGatewayCredential", _SignedIn)
    return urls


def test_open_opens_the_configured_hosted_address(opened: list[str]) -> None:
    result = CliRunner().invoke(cli_dashboard.dashboard_open, [])

    assert result.exit_code == 0, result.output
    assert opened == [f"{_DEV}/"]


def test_open_reads_the_address_from_configuration(
    opened: list[str], tmp_path: Path
) -> None:
    _config(tmp_path, "https://stage.example.test")

    CliRunner().invoke(cli_dashboard.dashboard_open, [])

    assert opened == ["https://stage.example.test/"]


def test_open_with_no_configured_address_refuses_and_opens_nothing(
    opened: list[str], tmp_path: Path
) -> None:
    _config(tmp_path, None)

    result = CliRunner().invoke(cli_dashboard.dashboard_open, [])

    assert result.exit_code != 0
    assert opened == []
    assert "dashboard.url" in result.output


def test_open_starts_no_local_server(
    opened: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_onex_setup_effect.handlers import (
        handler_setup_dashboard,
    )

    def boom(*a: Any, **k: Any) -> None:
        raise AssertionError("a local dashboard was touched")

    monkeypatch.setattr(handler_setup_dashboard.HandlerSetupDashboard, "apply", boom)
    monkeypatch.setattr(handler_setup_dashboard.HandlerSetupDashboard, "observe", boom)

    result = CliRunner().invoke(cli_dashboard.dashboard_open, [])

    assert result.exit_code == 0, result.output
    assert "127.0.0.1" not in opened[0]


def test_not_signed_in_says_run_onex_login_and_opens_nothing(
    opened: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_dashboard, "StoreGatewayCredential", _SignedOut)

    result = CliRunner().invoke(cli_dashboard.dashboard_open, [])

    assert result.exit_code != 0
    assert "onex login" in result.output
    assert opened == []


def test_a_correlation_id_opens_that_runs_workflow_page(opened: list[str]) -> None:
    result = CliRunner().invoke(
        cli_dashboard.dashboard_open, ["--correlation-id", "run&x=1"]
    )

    assert result.exit_code == 0, result.output
    parsed = urllib.parse.urlsplit(opened[0])
    assert parsed.path == "/workflow"
    assert urllib.parse.parse_qs(parsed.query) == {"correlation_id": ["run&x=1"]}
