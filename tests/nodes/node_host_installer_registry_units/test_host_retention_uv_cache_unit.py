# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_registry_units: the host retention uv cache systemd unit."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from tests.nodes.node_host_installer_registry_units.test_registry_units import (
    Recorder,
    make_checkout,
    request,
)

pytestmark = pytest.mark.unit

LINUX_PYTHON = "/usr/bin/python3.12"
HANDLER = "omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units"
UNIT = "host-retention-uv-cache-systemd"
MODULE = "omnimarket.nodes.node_host_retention_effect.runtime_host_retention_tick"
ENTRY = "src/omnimarket/nodes/node_host_retention_effect/runtime_host_retention_tick.py"
CREDENTIAL_NAME = re.compile(
    r"TOKEN|SECRET|PASSWORD|CREDENTIAL|API_KEY|PRIVATE_KEY", re.I
)


@pytest.fixture(autouse=True)
def linux_python(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        f"{HANDLER}.interpreter_present", lambda path: path == LINUX_PYTHON
    )
    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )


def test_golden_the_unit_declares_its_module_overlay_root_and_host_input() -> None:
    unit = load_units()[UNIT]
    assert unit["platform"] == "systemd"
    assert unit["module"] == MODULE
    assert unit["entry_file"] == ENTRY
    assert unit["probe_argv"] == ["--probe"]
    assert unit["environment"]["ONEX_SKILL_OVERLAY_ROOTS"] == "{overlays_root}"
    assert unit["environment_inputs"] == {"ONEX_HOST_NAME": "ledger_host_name"}
    assert unit["required_environment_inputs"] == ["ONEX_HOST_NAME"]
    assert unit["environment_path_overrides"] == ["ONEX_SKILL_OVERLAY_ROOTS"]
    assert unit["environment_refuse_worktree_paths"] == ["ONEX_SKILL_OVERLAY_ROOTS"]
    assert unit["schedule"]["start_interval"] == 21600
    assert unit["schedule"]["run_timeout_s"] == 3600
    assert unit["schedule"]["run_at_load"] is False
    assert (
        unit["evidence_file"]
        == ".local/state/omni/host-retention-uv-cache/last-run.json"
    )
    assert unit["state_directory"] == ".onex_state/host-retention-uv-cache"
    assert not CREDENTIAL_NAME.search("\n".join(unit["environment"]))


def test_golden_the_rendered_service_passes_the_template_arguments(
    tmp_path: Path,
) -> None:
    unit = load_units()[UNIT]
    branch = tmp_path / "branch"
    revision = make_checkout(branch, entry=unit["entry_file"])
    recorder = Recorder()
    handler = HandlerRegistryUnits(recorder)
    result = handler.handle(
        request(
            tmp_path,
            unit=UNIT,
            mode="dry-run",
            project_path=str(branch),
            expected_revision=revision,
        )
    )
    assert result.status == "success", result.stderr
    exec_line = next(
        line for line in result.rendered.splitlines() if line.startswith("ExecStart=")
    )
    omni = str(tmp_path / "omni")
    evidence = str((tmp_path / "home" / unit["evidence_file"]).parent)
    assert (
        f'"python" "-m" "{MODULE}" "--omni-home" "{omni}" "--state-dir" "{evidence}"'
        in exec_line
    )
    assert handler.tick_arguments(
        request(tmp_path, unit=UNIT, mode="dry-run"), unit
    ) == ["--omni-home", omni, "--state-dir", evidence]
