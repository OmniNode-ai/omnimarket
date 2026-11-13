# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_registry_units: the lab disk hygiene systemd unit (OMN-23707)."""

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
UNIT = "lab-disk-hygiene-systemd"
MODULE = "omnimarket.nodes.node_lab_disk_hygiene_effect"
ENTRY = "src/omnimarket/nodes/node_lab_disk_hygiene_effect/__main__.py"
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


def test_golden_the_lab_collectors_unit_declares_its_module_and_overlay_root() -> None:
    unit = load_units()[UNIT]
    assert unit["platform"] == "systemd"
    assert unit["module"] == MODULE
    assert unit["entry_file"] == ENTRY
    assert unit["probe_argv"] == ["--probe"]
    assert unit["environment"]["ONEX_SKILL_OVERLAY_ROOTS"] == "{overlays_root}"
    assert unit["environment_path_overrides"] == ["ONEX_SKILL_OVERLAY_ROOTS"]
    assert unit["environment_refuse_worktree_paths"] == ["ONEX_SKILL_OVERLAY_ROOTS"]
    assert unit["schedule"]["start_interval"] == 21600
    assert unit["schedule"]["run_timeout_s"] == 3600
    assert unit["schedule"]["run_at_load"] is False
    assert unit["evidence_file"] == ".local/state/omni/lab-disk-hygiene/last-run.json"
    assert unit["state_directory"] == ".onex_state/lab-disk-hygiene"
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
