# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_registry_units: the host job claim read is a declared systemd unit (OMN-25093).

The unit runs node_host_job_claim_compute's tick on the host it is installed on. The test checks the unit against the node's
contract (the seam: the module, the entry file, the probe and the topic the tick publishes), renders it, and shows the
install refused without the host name the tick needs.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from tests.nodes.node_host_installer_registry_units import (
    test_registry_units_systemd as base,
)

UNIT = "host-job-claim-systemd"
MODULE = "omnimarket.nodes.node_host_job_claim_compute.runtime_host_job_claim_tick"
ENTRY = (
    "src/omnimarket/nodes/node_host_job_claim_compute/runtime_host_job_claim_tick.py"
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def linux_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        f"{base.HANDLER}.interpreter_present", lambda path: path == base.LINUX_PYTHON
    )
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )


def _checkout(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "branch"
    base.make_checkout(path)
    (path / ENTRY).parent.mkdir(parents=True, exist_ok=True)
    (path / ENTRY).write_text("print('measurement')\n", encoding="utf-8")
    base._git(["git", "-C", str(path), "add", "-A"], check=True)
    base._git(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "commit",
            "-q",
            "-m",
            "y",
        ],
        check=True,
    )
    revision = base._git(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return path, revision


def test_the_unit_runs_the_nodes_tick_and_reads_its_contract() -> None:
    unit = load_units()[UNIT]
    contract = yaml.safe_load(
        (
            base.ROOT / "src/omnimarket/nodes/node_host_job_claim_compute/contract.yaml"
        ).read_text(encoding="utf-8")
    )
    assert unit["label"] == "onex-host-job-claim"
    assert unit["schedule"]["start_interval"] == 3600
    assert unit["module"] == MODULE
    assert unit["entry_file"] == ENTRY
    assert unit["probe_argv"] == ["--probe"]
    assert unit["required_environment_inputs"] == ["ONEX_LEDGER_HOST_NAME"]
    # The tick publishes the run-completed topic; the contract declares it as a publish topic and its terminal event.
    topic = contract["topics"]["host_job_claim_run_completed"]
    assert topic in contract["event_bus"]["publish_topics"]
    assert contract["terminal_event"] == topic
    node_dir = base.ROOT / "src/omnimarket/nodes/node_host_installer_registry_units"
    assert (node_dir / str(unit["template"])).is_file()
    assert (node_dir / str(unit["timer_template"])).is_file()
    assert (base.ROOT / ENTRY).is_file()


def test_dry_run_renders_an_hourly_timer_naming_the_host(tmp_path: Path) -> None:
    path, revision = _checkout(tmp_path)
    result = HandlerRegistryUnits(base.Recorder()).handle(
        base.request(
            tmp_path,
            unit=UNIT,
            mode="dry-run",
            project_path=str(path),
            expected_revision=revision,
            path_overrides={},
            workspace_config_root=str(tmp_path / "config-root"),
            ledger_host_name="h053",
        )
    )
    assert result.status == "success", result.stderr
    exec_line = next(
        line for line in result.rendered.splitlines() if line.startswith("ExecStart=")
    )
    assert f'"-m" "{MODULE}"' in exec_line
    assert f'"--project" "{path}"' in exec_line
    assert "OnUnitActiveSec=3600" in result.rendered
    assert "Unit=onex-host-job-claim.service" in result.rendered
    assert '"ONEX_LEDGER_HOST_NAME=h053"' in result.rendered
    assert "__" not in result.rendered.replace("__init__", "")


def test_the_install_is_refused_without_the_host_name_the_tick_needs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failing control: with no host name the tick would end INDETERMINATE every hour, so it must not install."""
    monkeypatch.delenv("ONEX_LEDGER_HOST_NAME", raising=False)
    path, revision = _checkout(tmp_path)
    result = HandlerRegistryUnits(base.Recorder()).handle(
        base.request(
            tmp_path,
            unit=UNIT,
            mode="install",
            project_path=str(path),
            expected_revision=revision,
            path_overrides={},
            workspace_config_root=str(tmp_path / "config-root"),
        )
    )
    assert result.status != "success"
    assert "ONEX_LEDGER_HOST_NAME" in result.stderr
