# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26226: how the setup lifecycle hands the start token to the dashboard process.

The token travels in a file the handler creates, never in argv; the child is told
the file's path by one environment name, and the file is always created fresh at
0o600, even when something already sits at that path.
"""

from __future__ import annotations

import io
import os
import stat
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

_HANDLER = "omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_dashboard"
_PLANTED = "planted-handoff-token-7c1d"


def _apply(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    before_start=None,
) -> tuple[Path, dict[str, object]]:
    """Run apply() with a fake child; return the handler's home and what the child saw."""
    home = tmp_path / "home"
    home.mkdir()
    onex_home = home / ".onex"
    onex_home.mkdir(mode=0o700)
    store = LocalByokCredentialStore(db_path=home / "delegation.sqlite")
    mint_local_tenant_identity(db_path=store.db_path)
    handler = HandlerSetupDashboard(onex_home=onex_home, store=store)
    monkeypatch.setattr(f"{_HANDLER}.mint_dashboard_token", lambda: SecretStr(_PLANTED))
    if before_start is not None:
        before_start(onex_home)
    seen: dict[str, object] = {}

    def fake_popen(command, **kwargs):
        env = kwargs["env"]
        seen["env_path"] = env.get("ONEX_DASHBOARD_TOKEN_FILE")
        if seen["env_path"]:
            path = Path(str(seen["env_path"]))
            seen["content"] = path.read_text(encoding="utf-8")
            seen["mode"] = stat.S_IMODE(path.stat().st_mode)
            seen["inode"] = path.stat().st_ino

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

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(f"{_HANDLER}._stamp", lambda _pid: "planted-stamp")
    monkeypatch.setattr(handler, "_ready", lambda _record: True)
    monkeypatch.setattr(
        f"{_HANDLER}.select.select", lambda r, _w, _x, _t=0: (r, [], [])
    )
    handler.apply(ModelSetupPlan(workspace=tmp_path, components=("dashboard",)))
    return onex_home, seen


def test_the_child_is_told_the_token_file_under_the_handlers_own_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    onex_home, seen = _apply(monkeypatch, tmp_path)

    assert seen["env_path"] == str(onex_home / "dashboard.token")
    assert seen["content"] == _PLANTED
    assert seen["mode"] == 0o600


def test_a_wider_file_already_at_the_path_never_receives_the_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    kept = tmp_path / "kept-link"

    def plant(onex_home: Path) -> None:
        stale = onex_home / "dashboard.token"
        stale.write_text("old-secret", encoding="utf-8")
        stale.chmod(0o644)
        os.link(stale, kept)  # keeps the old file alive so a rewrite in place shows

    _, seen = _apply(monkeypatch, tmp_path, before_start=plant)

    assert kept.read_text(encoding="utf-8") == "old-secret"
    assert stat.S_IMODE(kept.stat().st_mode) == 0o644
    assert seen["content"] == _PLANTED
    assert seen["mode"] == 0o600
    assert not os.path.exists(str(seen["env_path"]))
