# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The macOS (launchd) pool rotation tick unit keeps its logs and its working directory off the registry root.

h101's registry root links to an external volume; launchd opened the unit's log files there and ended the job with
exit 78 (EX_CONFIG) and no log. The unit declares log_directory and launchd_root, both relative to the home, the way
the overlay store reader unit does.
"""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelRegistryUnitsRequest,
)
from tests.nodes.node_host_installer_registry_units.test_registry_units import (
    BREW_PYTHON,
    make_checkout,
    plist_of,
    real_run,
    request,
)

UNIT = "claude-pool-rotation-tick"
SYSTEMD_UNIT = "claude-pool-rotation-tick-systemd"
ENTRY = "src/omnimarket/nodes/node_claude_token_pool_effect/runtime_claude_pool_tick.py"


@pytest.fixture(autouse=True)
def brew_python(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units.interpreter_present",
        lambda path: path == BREW_PYTHON,
    )


def render(tmp_path: Path, unit: str, entry: str | None, **kw: Any) -> dict[str, Any]:
    branch = tmp_path / "branch"
    revision = make_checkout(branch, entry=entry) if entry else make_checkout(branch)
    result = HandlerRegistryUnits(real_run).handle(
        request(
            tmp_path,
            unit=unit,
            project_path=str(branch),
            expected_revision=revision,
            **kw,
        )
    )
    assert result.status == "success", result.stderr
    return plist_of(result.rendered)


def test_the_launchd_pool_tick_declares_a_log_directory_and_a_launchd_root() -> None:
    unit = load_units()[UNIT]
    assert unit["log_directory"] == ".local/state/omni/claude-pool-rotation-tick"
    assert unit["launchd_root"] == ".local/state/omni/claude-pool-rotation-tick"


def test_golden_the_pool_tick_logs_and_working_directory_sit_under_the_home(
    tmp_path: Path,
) -> None:
    plist = render(
        tmp_path,
        UNIT,
        ENTRY,
        ledger_host_name="h101",
        path_overrides={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "overlays")},
    )
    logs = tmp_path / "home/.local/state/omni/claude-pool-rotation-tick"
    assert plist["StandardOutPath"] == str(logs / "claude-pool-rotation-tick.out.log")
    assert plist["StandardErrorPath"] == str(logs / "claude-pool-rotation-tick.err.log")
    assert plist["WorkingDirectory"] == str(logs)
    assert str(tmp_path / "omni") not in plist["StandardOutPath"]


def test_the_linux_unit_is_unchanged() -> None:
    unit = load_units()[SYSTEMD_UNIT]
    assert "log_directory" not in unit
    assert "launchd_root" not in unit


def test_error_a_launchd_unit_without_the_keys_keeps_the_registry_root(
    tmp_path: Path,
) -> None:
    """Failing control: a unit that declares neither key keeps its logs and working directory in the registry root."""
    declared = dict(load_units()[UNIT])
    declared.pop("log_directory")
    declared.pop("launchd_root")
    handler = HandlerRegistryUnits(
        lambda argv: subprocess.CompletedProcess(argv, 0, "test-user", "")
    )
    req = ModelRegistryUnitsRequest(
        unit=UNIT,
        home=str(tmp_path / "home"),
        registry_root=str(tmp_path / "omni"),
        workspace_config_root=str(tmp_path / "ws"),
        plans_path=str(tmp_path / "plans"),
        marketplace_root=str(tmp_path / "market"),
        runtime_ssh_host="lab-runtime",
        ledger_host_name="h101",
        path_overrides={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "overlays")},
    )
    text = handler.render(req, declared, tmp_path, "uv", "python")
    plist = plistlib.loads(text.encode())
    assert plist["StandardOutPath"].startswith(str(tmp_path / "omni"))
