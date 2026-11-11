# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Installed CLI must accept a package capability absent from its source lists.

Failure controls: Click must not reject an unknown installed component; rerunning
must preserve its owned marker and journal; unavailable components must fail.
The artifact records both installed command runs and the unchanged snapshot.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.helpers.setup_shell_environment import requires_supported_macos

pytestmark = pytest.mark.e2e


@requires_supported_macos
def test_installed_setup_accepts_external_component(tmp_path: Path) -> None:
    package = tmp_path / "packages"
    package.mkdir()
    metadata = package / "customer_setup-1.0.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: customer-setup\nVersion: 1.0.0\n"
    )
    (metadata / "entry_points.txt").write_text(
        "[onex.setup]\ncustomer = customer_setup:PLUGIN\n"
    )
    (package / "customer_setup.py").write_text("""
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_observation import ModelSetupObservation
PLUGIN = {"id": "customer", "api_version": 1, "version": "1.0.0", "components": ["customer"], "requires": ["client"], "steps": [{"name": "customer", "factory": "customer_setup:Effect", "context": ["onex_home"]}]}
class Effect:
    name = "customer"
    def __init__(self, *, onex_home): self.marker = onex_home / "customer-marker"
    def observe(self, plan): return ModelSetupObservation(step=self.name, status="success" if self.marker.exists() else "unavailable")
    def apply(self, plan): self.marker.write_text("enabled")
""")
    home = tmp_path / "home"
    home.mkdir()
    environment = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(package),
    }
    command = [
        str(Path(sys.executable).parent / "onex"),
        "setup",
        "--no-interactive",
        "--yes",
        "--json",
        "--workspace",
        str(tmp_path / "workspace"),
        "--component",
        "customer",
    ]
    first = subprocess.run(
        command,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert first.returncode == 0, first.stdout + first.stderr
    state = home / ".onex" / "setup.json"
    marker = home / ".onex" / "customer-marker"
    snapshot = (state.read_bytes(), marker.stat().st_mtime_ns)
    second = subprocess.run(
        command,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert second.returncode == 0, second.stdout + second.stderr
    assert snapshot == (state.read_bytes(), marker.stat().st_mtime_ns)
    rejected = subprocess.run(
        [*command[:-1], "uninstalled-component"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert rejected.returncode != 0
    assert snapshot == (state.read_bytes(), marker.stat().st_mtime_ns)
    (tmp_path / "plugin-evidence.json").write_text(
        json.dumps(
            {
                "first": json.loads(first.stdout),
                "second": json.loads(second.stdout),
                "unknown_component_exit": rejected.returncode,
                "unchanged": True,
            },
            indent=2,
        )
    )
