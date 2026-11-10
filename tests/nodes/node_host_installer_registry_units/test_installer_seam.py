# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_registry_units: the seams every install reads (OMN-23574).

Every install a lane or the orchestrator makes sends this node one request and reads one result. Three seams:

1. The request and result shapes. A request is a JSON document piped to
   ``python -m omnimarket.nodes.node_host_installer_registry_units`` (a file/stream seam: the installing shell writes
   it and the node reads it; there is no topic in that path) or published on the command topic. A consumer's request
   below is written out by hand, the way a consumer writes it, so a renamed or removed request field fails here.
2. The topics the contract binds. A consumer that publishes or reads the terminal event names them as strings.
3. The environment variable names the cutover units declare against the names the nodes they run read: a unit that
   pins a file under a name its node does not read installs and then runs on the wrong overlay.
"""

from __future__ import annotations

import io
import json
import shutil
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_host_installer_registry_units.__main__ import main
from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    OVERLAY_ROOTS_ENV,
    load_units,
)
from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelRegistryUnitsRequest,
    ModelRegistryUnitsResult,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.models.model_snapshot_overlay import (
    OVERLAY_FILE_ENV,
)
from tests.nodes.node_host_installer_registry_units.test_registry_units import (
    make_checkout,
)

pytestmark = pytest.mark.unit

TICK_ENTRY = "src/omnimarket/nodes/node_pr_landing_tick_orchestrator/__main__.py"

NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_host_installer_registry_units"
)

REQUEST_FIELDS = {
    "unit",
    "mode",
    "home",
    "registry_root",
    "workspace_config_root",
    "plans_path",
    "marketplace_root",
    "source_root",
    "ledger_path",
    "project_path",
    "expected_revision",
    "path_overrides",
    "runtime_ssh_host",
    "phoenix_url",
    "appender_root",
    "host_table_path",
    "ledger_host_name",
    "remote_lane_runner",
    "alert_env_file",
}
RESULT_FIELDS = {
    "unit",
    "mode",
    "status",
    "exit_code",
    "stdout",
    "stderr",
    "rendered",
    "project",
    "revision",
    "bundle_sha256",
    "project_is_canonical",
    "record",
    "config",
    "config_path",
}
COMMAND_TOPIC = "onex.cmd.omnimarket.host-registry-unit-requested.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.host-registry-unit-completed.v1"


def consumer_request(tmp_path: Path) -> str:
    """The request an install makes for the cutover units, written field by field as a consumer writes it."""
    return json.dumps(
        {
            "unit": "landing-tick-orchestrator",
            "mode": "dry-run",
            "home": str(tmp_path / "home"),
            "registry_root": str(tmp_path / "omni"),
            "workspace_config_root": str(tmp_path / "ws"),
            "marketplace_root": str(tmp_path / "market"),
            "project_path": "",
            "expected_revision": "",
            "path_overrides": {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "overlays")},
        }
    )


def test_the_request_and_result_keep_the_fields_every_install_reads() -> None:
    assert set(ModelRegistryUnitsRequest.model_fields) == REQUEST_FIELDS
    assert set(ModelRegistryUnitsResult.model_fields) == RESULT_FIELDS
    # a consumer's extra or misspelled field is refused, never ignored
    assert ModelRegistryUnitsRequest.model_config["extra"] == "forbid"
    assert ModelRegistryUnitsResult.model_config["extra"] == "forbid"


def test_a_consumer_request_is_read_and_answered_through_the_node_entry_point(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    make_checkout(tmp_path / "omni" / "omnimarket", entry=TICK_ENTRY)
    monkeypatch.setattr("sys.stdin", io.StringIO(consumer_request(tmp_path)))
    monkeypatch.setattr(
        "omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units.interpreter_present",
        lambda path: path == "/opt/homebrew/bin/python3.13",
    )
    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )
    assert main() == 0
    result = ModelRegistryUnitsResult.model_validate_json(capsys.readouterr().out)
    assert result.status == "success", result.stderr
    assert result.unit == "landing-tick-orchestrator"
    assert f"{tmp_path / 'overlays'}" in result.rendered


def test_the_contract_binds_the_topics_a_consumer_names() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert contract["event_bus"]["publish_topics"] == [TERMINAL_TOPIC]
    assert contract["terminal_event"] == TERMINAL_TOPIC
    assert contract["externally_consumed_topics"] == [TERMINAL_TOPIC]


def test_the_cutover_units_declare_the_environment_names_their_nodes_read() -> None:
    units = load_units()
    snapshot = units["landing-snapshot-cutover"]
    assert OVERLAY_FILE_ENV in snapshot["environment"]
    assert OVERLAY_FILE_ENV in snapshot["environment_refuse_worktree_paths"]
    assert snapshot["environment"][OVERLAY_FILE_ENV].startswith("{overlay_root}/")
    for name in (
        "landing-watcher-facts",
        "landing-snapshot-cutover",
        "landing-tick-orchestrator",
    ):
        assert OVERLAY_ROOTS_ENV in units[name]["environment"]
        assert OVERLAY_ROOTS_ENV in units[name]["environment_path_overrides"]
