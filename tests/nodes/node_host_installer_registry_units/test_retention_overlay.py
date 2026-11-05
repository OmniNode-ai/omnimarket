# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A Mac retention timer is deployment data consumed by the existing installer."""

from __future__ import annotations

import json
import plistlib
import subprocess
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_host_installer_registry_units.handlers import (
    handler_registry_units as installer,
)
from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelRegistryUnitsRequest,
)

UNIT = {
    "platform": "launchd",
    "kind": "registry_module_tick",
    "label": "ai.omninode.retention-cleanup",
    "template": "templates/ai.omninode.registry-module-tick.plist.tmpl",
    "schedule": {
        "start_interval": 3600,
        "run_at_load": True,
        "description": "hourly and at load",
    },
    "registry_project": "omnimarket",
    "interpreter_candidates": ["/usr/local/bin/python3.13"],
    "module": "omnimarket.nodes.node_retention_cleanup",
    "entry_file": "src/omnimarket/nodes/node_retention_cleanup/__main__.py",
    "project_path_override": {
        "request_field": "project_path",
        "requires": "clean checkout",
    },
    "arguments": ["--files", "--only", "lab-run-lane-dirs", "lab-run-context-dirs"],
    "evidence_file": ".local/state/omni/retention-cleanup/last-run.json",
    "state_directory": ".onex_state/retention-cleanup",
}


def overlay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "overlays"
    path = root / "node_host_installer_registry_units" / "overlay.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump({"units": {"retention-cleanup": UNIT}}))
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))
    return path


def test_deployment_overlay_declares_mac_retention_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay(tmp_path, monkeypatch)
    units = installer.load_units()
    assert units["retention-cleanup"]["platform"] == "launchd"
    assert "factory-floor-publish" in units


def test_retention_timer_runs_files_and_records_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay(tmp_path, monkeypatch)
    monkeypatch.setattr(installer, "interpreter_present", lambda _path: True)
    monkeypatch.setattr(installer.shutil, "which", lambda _name: "/usr/bin/uv")

    def run(argv):
        stdout = "fixture\n" if argv[:2] == ["id", "-un"] else "deadbeef\n"
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    result = installer.HandlerRegistryUnits(run=run).handle(
        ModelRegistryUnitsRequest(
            unit="retention-cleanup",
            home=str(tmp_path / "home"),
            registry_root=str(tmp_path / "registry"),
        )
    )
    assert result.status == "success", result.stderr
    plist = plistlib.loads(result.rendered.encode())
    args = plist["ProgramArguments"]
    assert "--files" in args
    assert args[args.index("--only") + 1 :] == [
        "lab-run-lane-dirs",
        "lab-run-context-dirs",
    ]
    assert plist["StartInterval"] == 3600
    assert plist["RunAtLoad"] is True


def test_host_tick_accepts_rendered_arguments_and_writes_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_retention_cleanup import __main__ as cli

    home = tmp_path / "home"
    runs = home / "lab-run" / "runs"
    runs.mkdir(parents=True)
    state = home / ".local/state/omni/retention-cleanup"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OMNIDASH_ANALYTICS_DB_URL", raising=False)
    monkeypatch.setattr(
        "sys.argv",
        [
            "retention-cleanup",
            "--omni-home",
            str(tmp_path / "registry"),
            "--state-dir",
            str(state),
            *UNIT["arguments"],
        ],
    )
    cli.main()
    receipt = json.loads((state / "last-run.json").read_text())
    assert receipt["status"] == "ok"
    assert {item["name"] for item in receipt["files"]["items"]} == {
        "lab-run-lane-dirs",
        "lab-run-context-dirs",
    }


@pytest.mark.asyncio
async def test_retention_overlay_through_runtime_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    from uuid import uuid4

    from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
    from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
    from omnibase_infra.runtime.auto_wiring.discovery import (
        discover_contracts_from_paths,
    )
    from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
    from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

    from omnimarket.nodes.node_host_installer_registry_units.node import (
        NodeHostInstallerRegistryUnits,
    )

    overlay(tmp_path, monkeypatch)
    monkeypatch.setattr(installer, "interpreter_present", lambda _path: True)
    monkeypatch.setattr(installer.shutil, "which", lambda _name: "/usr/bin/uv")
    contract_path = NodeHostInstallerRegistryUnits.contract_path
    manifest = discover_contracts_from_paths([contract_path])
    assert not manifest.errors
    assert len(manifest.contracts) == 1
    contract = yaml.safe_load(contract_path.read_text())
    command = contract["runtime_dispatch"]["command_topic"]
    completed = contract["terminal_event"]
    results: asyncio.Queue[dict] = asyncio.Queue()
    bus = EventBusInmemory(environment="local", group="retention-overlay")
    await bus.start()

    async def collect(message):
        await results.put(json.loads(message.value))

    try:
        await bus.subscribe(completed, group_id="retention-results", on_message=collect)
        engine = MessageDispatchEngine()
        report = await wire_from_manifest(
            manifest, engine, event_bus=bus, environment="local"
        )
        assert [result.outcome.value for result in report.results] == ["wired"]
        engine.freeze()
        request = ModelRegistryUnitsRequest(
            unit="retention-cleanup",
            home=str(tmp_path / "home"),
            registry_root=str(tmp_path / "registry"),
        )
        correlation = uuid4()
        envelope = ModelEventEnvelope[object](
            payload=request.model_dump(),
            correlation_id=correlation,
            event_type="omnimarket.host-installer-registry-units-requested",
        )
        await bus.publish(command, None, envelope.model_dump_json().encode(), None)
        event = await asyncio.wait_for(results.get(), timeout=5)
        assert event["correlation_id"] == str(correlation)
        assert event["payload"]["status"] == "success"
        arguments = plistlib.loads(event["payload"]["rendered"].encode())[
            "ProgramArguments"
        ]
        assert "--files" in arguments
    finally:
        await bus.close()
