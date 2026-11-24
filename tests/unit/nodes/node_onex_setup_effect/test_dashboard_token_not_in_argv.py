# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC3 (OMN-26226): the setup lifecycle never puts the session token in argv."""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest
from pydantic import SecretStr

from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore
from omnimarket.local_deployment.tenant_identity import mint_local_tenant_identity
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_dashboard import (
    HandlerSetupDashboard,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)

pytestmark = pytest.mark.unit


def test_apply_does_not_place_the_planted_token_in_the_child_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    store = LocalByokCredentialStore(db_path=home / "delegation.sqlite")
    mint_local_tenant_identity(db_path=store.db_path)
    planted = "planted-session-token-must-stay-out-of-argv"
    handler = HandlerSetupDashboard(onex_home=home / ".onex", store=store)
    monkeypatch.setattr(
        "omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_dashboard.mint_dashboard_token",
        lambda: SecretStr(planted),
    )

    def fake_popen(command, **kwargs):
        captured["command"] = list(command)

        class _Process:
            pid = 43210

            def __init__(self) -> None:
                self.stdout = io.BytesIO(b"onex dashboard: http://127.0.0.1:12345/\n")

            def poll(self):
                return None

            def terminate(self):
                return None

            def wait(self, timeout=None):
                return 0

            def kill(self):
                return None

        return _Process()

    captured: dict[str, list[str]] = {}
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        "omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_dashboard._stamp",
        lambda _pid: "planted-stamp",
    )
    monkeypatch.setattr(handler, "_ready", lambda _record: True)
    monkeypatch.setattr(
        "omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_dashboard.select.select",
        lambda r, _w, _x, _t=0: (r, [], []),
    )

    handler.apply(ModelSetupPlan(workspace=tmp_path, components=("dashboard",)))

    argv = " ".join(captured["command"])
    assert planted not in argv
    assert "--access-token" not in captured["command"]
    assert not any("planted-session-token" in part for part in captured["command"])
