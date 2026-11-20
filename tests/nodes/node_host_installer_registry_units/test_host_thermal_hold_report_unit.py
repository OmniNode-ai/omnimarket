# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_registry_units: the host thermal hold report unit (OMN-19998).

The unit runs the report on a host whose thermal guard closes the lab-run folder: it delivers the guard's queued rows and
posts one MSG per trip and release. It must render with the host's name, the declared ledger append command and the module,
refuse an install that names no host, refuse a checkout at another revision, and answer its probe without a write.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
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

UNIT = "host-thermal-hold-report-systemd"
HANDLER = "omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units"
LINUX_PYTHON = "/usr/bin/python3.12"
CREDENTIAL_NAME = re.compile(
    r"TOKEN|SECRET|PASSWORD|CREDENTIAL|API_KEY|PRIVATE_KEY", re.I
)
REPO_SRC = Path(__file__).resolve().parents[3] / "src"


@pytest.fixture(autouse=True)
def python_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        f"{HANDLER}.interpreter_present", lambda path: path == LINUX_PYTHON
    )
    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )


def test_golden_the_unit_renders_with_the_host_name_the_ledger_command_and_the_module(
    tmp_path: Path,
) -> None:
    unit = load_units()[UNIT]
    assert unit["platform"] == "systemd"
    branch = tmp_path / "branch"
    revision = make_checkout(branch, entry=unit["entry_file"])
    result = HandlerRegistryUnits(Recorder()).handle(
        request(
            tmp_path,
            unit=UNIT,
            mode="dry-run",
            project_path=str(branch),
            expected_revision=revision,
            ledger_host_name="host-a",
        )
    )
    assert result.status == "success", result.stderr
    assert result.revision == revision
    assert not result.project_is_canonical
    environment = [
        line for line in result.rendered.splitlines() if line.startswith("Environment=")
    ]
    assert any("ONEX_HOST_NAME=host-a" in line for line in environment)
    assert any(
        "ONEX_LEDGER_BUS_APPEND_COMMAND=" in line
        and "work_ledger_bus.cli append" in line
        for line in environment
    )
    assert unit["module"] in result.rendered
    assert not CREDENTIAL_NAME.search("\n".join(environment))


def test_error_an_install_without_the_host_name_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONEX_LEDGER_HOST_NAME", raising=False)
    monkeypatch.delenv("ONEX_HOST_NAME", raising=False)
    unit = load_units()[UNIT]
    assert unit["required_environment_inputs"] == ["ONEX_HOST_NAME"]
    with pytest.raises(ValueError, match="ONEX_HOST_NAME is unset"):
        HandlerRegistryUnits(Recorder()).environment(
            request(tmp_path, unit=UNIT, mode="install", ledger_host_name=""), unit
        )


def test_error_a_checkout_at_another_revision_is_refused(tmp_path: Path) -> None:
    unit = load_units()[UNIT]
    branch = tmp_path / "branch"
    make_checkout(branch, entry=unit["entry_file"])
    result = HandlerRegistryUnits(Recorder()).handle(
        request(
            tmp_path,
            unit=UNIT,
            mode="dry-run",
            project_path=str(branch),
            expected_revision="0" * 40,
            ledger_host_name="host-a",
        )
    )
    assert result.status != "success"


def test_golden_the_unit_module_answers_its_probe_without_an_overlay_or_a_write(
    tmp_path: Path,
) -> None:
    unit = load_units()[UNIT]
    done = subprocess.run(
        [sys.executable, "-m", unit["module"], *unit["probe_argv"]],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(REPO_SRC)},
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {"status": "probe_ok"}
    assert list(tmp_path.iterdir()) == []
