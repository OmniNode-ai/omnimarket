# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_registry_units: the three landing units on a Linux pool host (OMN-23662).

The cutover landing units (OMN-23514) were declared for launchd only, so no lab host could run the new landing workflow
in observe mode beside the controller. Each now has a ``-systemd`` sibling: the same module, entry file, probe,
environment, evidence file and interval as its launchd unit, rendered as a systemd user service and timer. The service
passes the template arguments (``--omni-home``, ``--state-dir``) the mains accept, and names no write credential.
"""

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
SIBLINGS = {
    "landing-watcher-facts-systemd": "landing-watcher-facts",
    "landing-snapshot-cutover-systemd": "landing-snapshot-cutover",
    "landing-tick-orchestrator-systemd": "landing-tick-orchestrator",
}
SHARED_FIELDS = (
    "module",
    "entry_file",
    "probe_argv",
    "environment",
    "environment_path_overrides",
    "environment_refuse_worktree_paths",
    "evidence_file",
    "state_directory",
    "registry_project",
    "tick_no_sync",
)
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


@pytest.mark.parametrize(("systemd", "launchd"), sorted(SIBLINGS.items()))
def test_golden_each_systemd_unit_mirrors_its_launchd_sibling(
    systemd: str, launchd: str
) -> None:
    units = load_units()
    declared, sibling = units[systemd], units[launchd]
    assert declared["platform"] == "systemd"
    for field in SHARED_FIELDS:
        assert declared.get(field) == sibling.get(field), field
    assert (
        declared["schedule"]["start_interval"] == sibling["schedule"]["start_interval"]
    )
    assert declared["schedule"]["run_timeout_s"] == sibling["run_timeout_s"]
    assert (
        declared["schedule"]["run_timeout_s"] < declared["schedule"]["start_interval"]
    )
    assert declared["probe_argv"] == ["--probe"]


@pytest.mark.parametrize("systemd", sorted(SIBLINGS))
def test_error_no_landing_unit_names_a_write_credential(systemd: str) -> None:
    for name in load_units()[systemd]["environment"]:
        assert not CREDENTIAL_NAME.search(name), name


@pytest.mark.parametrize("systemd", sorted(SIBLINGS))
def test_golden_the_rendered_service_passes_the_template_arguments_and_the_probe_check_matches(
    tmp_path: Path, systemd: str
) -> None:
    unit = load_units()[systemd]
    branch = tmp_path / "branch"
    revision = make_checkout(branch, entry=unit["entry_file"])
    recorder = Recorder()
    handler = HandlerRegistryUnits(recorder)
    result = handler.handle(
        request(
            tmp_path,
            unit=systemd,
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
        f'"python" "-m" "{unit["module"]}" "--omni-home" "{omni}" "--state-dir" "{evidence}"'
        in exec_line
    )
    assert handler.tick_arguments(
        request(tmp_path, unit=systemd, mode="dry-run"), unit
    ) == ["--omni-home", omni, "--state-dir", evidence]
    assert "ONEX_LANDING_SNAPSHOT_OVERLAY_FILE" in result.rendered or systemd != (
        "landing-snapshot-cutover-systemd"
    )
    assert not CREDENTIAL_NAME.search(
        "\n".join(
            line
            for line in result.rendered.splitlines()
            if line.startswith("Environment=")
        )
    )
