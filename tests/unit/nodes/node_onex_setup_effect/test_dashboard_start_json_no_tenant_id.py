# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for OMN-26226 AC4: the ``onex dashboard start --json`` path.

Catches these failure modes:
- the JSON evidence carries a ``tenant_id`` key;
- the full planted tenant id appears in output;
- the first block of the planted tenant id appears in output.
"""

import json
import types
from pathlib import Path
from uuid import UUID

import pytest
from click.testing import CliRunner

import omnimarket.nodes.node_onex_setup_effect.cli_dashboard as cli_dashboard
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_dashboard import (
    HandlerSetupDashboard,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_dashboard_process import (
    ModelSetupDashboardProcess,
)

pytestmark = pytest.mark.unit

_PLANTED_TENANT = "9c4b7e21-5d3a-4f68-b0e2-7a1c3d5e9f08"


@pytest.fixture
def patched_dashboard(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        cli_dashboard,
        "read_local_tenant_identity",
        lambda *_args: types.SimpleNamespace(tenant_uuid=UUID(_PLANTED_TENANT)),
    )
    monkeypatch.setattr(HandlerSetupDashboard, "apply", lambda *_args: None)
    monkeypatch.setattr(
        HandlerSetupDashboard,
        "_read",
        lambda *_args: ModelSetupDashboardProcess(
            pid=4242,
            process_stamp="stamp",
            url="http://127.0.0.1:18080",
            credential_ref="setup.dashboard.session",
            tenant_id=_PLANTED_TENANT,
        ),
    )
    monkeypatch.setattr(HandlerSetupDashboard, "_ready", lambda *_args: True)

    def _init(
        self: HandlerSetupDashboard,
        *,
        onex_home: Path | None = None,
        store: object | None = None,
    ) -> None:
        self._home = tmp_path
        self._state = tmp_path / "dashboard.json"
        self._store = None

    monkeypatch.setattr(HandlerSetupDashboard, "__init__", _init)


def test_dashboard_start_json_shows_no_tenant_id(patched_dashboard: None) -> None:
    runner = CliRunner()
    result = runner.invoke(cli_dashboard.dashboard_start, ["--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "success", "dashboard step did not succeed"
    evidence = payload["evidence"]
    assert evidence["url"] == "http://127.0.0.1:18080", "dashboard URL missing"
    assert "tenant_id" not in evidence, "tenant_id leaked in evidence"
    assert _PLANTED_TENANT not in result.output, "full tenant id leaked"
    assert _PLANTED_TENANT.split("-")[0] not in result.output, "tenant id block leaked"
