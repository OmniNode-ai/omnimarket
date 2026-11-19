# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex dashboard open --correlation-id`` opens that run's Workflow page (OMN-23413 AC47).

A delegated answer names its run, but the dashboard listens on a port chosen at
start and admits only this start's token, so a printed link cannot be stable.
The answer instead names one command, and the command builds the authenticated
address of the run's own Workflow page.

Each test names the failure it exists to catch:

* the command opens the dashboard's front page, not the named run, so the
  reader has to find the run by hand;
* a plain ``onex dashboard open`` changes behaviour;
* an id carrying ``&`` or ``=`` adds its own query parameter (a second
  ``token``) instead of staying one correlation id;
* an empty id opens the newest run while claiming to open the one asked for.
"""

from __future__ import annotations

import urllib.parse
from typing import Any

import click
import pytest
from click.testing import CliRunner

from omnimarket.inference import local_byok_credential_adapter
from omnimarket.nodes.node_onex_setup_effect import cli_dashboard
from omnimarket.nodes.node_onex_setup_effect.handlers import handler_setup_dashboard
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_observation import (
    ModelSetupObservation,
)

pytestmark = pytest.mark.unit

_URL = "http://127.0.0.1:49571"
_TOKEN = "start-token-0f2a"
_RUN = "445adc8a-1289-4459-9776-b9570d0d623b"


class _RunningDashboard:
    def observe(self, plan: Any) -> ModelSetupObservation:
        return ModelSetupObservation(
            step="dashboard",
            status="success",
            evidence={"url": _URL, "credential_ref": "dashboard-session"},
        )


class _Credentials:
    async def get_secret(self, ref: str) -> str:
        assert ref == "dashboard-session"
        return _TOKEN


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    urls: list[str] = []
    monkeypatch.setattr(
        handler_setup_dashboard, "HandlerSetupDashboard", _RunningDashboard
    )
    monkeypatch.setattr(
        local_byok_credential_adapter, "LocalByokCredentialStore", _Credentials
    )

    def launch(url: str, *args: Any, **kwargs: Any) -> int:
        urls.append(url)
        return 0

    monkeypatch.setattr(click, "launch", launch)
    return urls


def _open(*args: str) -> Any:
    return CliRunner().invoke(cli_dashboard.dashboard_open, list(args))


def _parts(url: str) -> tuple[str, dict[str, list[str]]]:
    parsed = urllib.parse.urlsplit(url)
    return (
        f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
        urllib.parse.parse_qs(parsed.query),
    )


def test_a_correlation_id_opens_that_runs_workflow_page(opened: list[str]) -> None:
    result = _open("--correlation-id", _RUN)

    assert result.exit_code == 0, result.output
    (url,) = opened
    page, query = _parts(url)
    assert page == f"{_URL}/workflow"
    assert query == {"correlation_id": [_RUN], "token": [_TOKEN]}


def test_without_an_id_open_still_opens_the_front_page(opened: list[str]) -> None:
    result = _open()

    assert result.exit_code == 0, result.output
    (url,) = opened
    page, query = _parts(url)
    assert page == f"{_URL}/"
    assert query == {"token": [_TOKEN]}


def test_an_id_cannot_add_its_own_query_parameters(opened: list[str]) -> None:
    hostile = "abc&token=attacker&x=1"

    result = _open("--correlation-id", hostile)

    assert result.exit_code == 0, result.output
    (url,) = opened
    _, query = _parts(url)
    assert query == {"correlation_id": [hostile], "token": [_TOKEN]}


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_id_is_refused_and_nothing_opens(opened: list[str], blank: str) -> None:
    result = _open("--correlation-id", blank)

    assert result.exit_code == 2, result.output
    assert "--correlation-id must not be blank" in result.output
    assert opened == []
