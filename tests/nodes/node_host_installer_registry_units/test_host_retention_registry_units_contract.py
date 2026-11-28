# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from __future__ import annotations

import importlib
import json
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
UNIT = "host-retention-systemd"
MODULE = "omnimarket.nodes.node_host_retention_effect.runtime_host_retention_preview"


def test_registry_units_contract_runs_host_retention_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    unit = load_units()[UNIT]
    assert unit["module"] == MODULE
    assert unit["label"] == "onex-host-retention"
    assert unit["platform"] == "systemd"
    assert "ONEX_HOST_NAME" in unit["required_environment_inputs"]
    monkeypatch.setattr(
        "omnimarket.nodes.node_host_installer_registry_units.handlers"
        ".handler_registry_units.interpreter_present",
        lambda path: path == "/usr/bin/python3.12",
    )
    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )

    branch = tmp_path / "branch"
    revision = make_checkout(branch, entry=unit["entry_file"])
    overlay_root = tmp_path / "overlays"
    host = "lab-test-host"
    install_request = request(
        tmp_path,
        unit=UNIT,
        mode="dry-run",
        project_path=str(branch),
        expected_revision=revision,
        host_name=host,
        path_overrides={"ONEX_SKILL_OVERLAY_ROOTS": str(overlay_root)},
    )
    installer = HandlerRegistryUnits(Recorder())
    result = installer.handle(install_request)
    assert result.status == "success", result.stderr
    assert f'ONEX_HOST_NAME={host}"' in result.rendered
    assert f'"python" "-m" "{MODULE}"' in result.rendered
    assert "OnActiveSec=1s" in result.rendered
    assert "StandardOutput=journal" in result.rendered
    assert "StandardError=journal" in result.rendered

    argv = installer.tick_arguments(install_request, unit)
    state_dir = Path(argv[argv.index("--state-dir") + 1])
    main = importlib.import_module(MODULE).main
    monkeypatch.delenv("ONEX_HOST_NAME", raising=False)
    assert main([*argv, *installer.probe_argv(unit)]) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "probe_ok"}
    assert not state_dir.exists()

    overlay = overlay_root / "node_host_retention_effect" / "overlay.yaml"
    overlay.parent.mkdir(parents=True)
    cache = tmp_path / "uv"
    cache.mkdir()
    fake_uv = tmp_path / "fake-uv"
    fake_uv.write_text(
        '#!/bin/sh\n[ "$*" = "cache size" ] || exit 1\nprintf "123 bytes\\n"\n',
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)
    overlay.write_text(
        f"hosts:\n  - name: {host}\n"
        "    docker_timeout_seconds: 20\n"
        "    uv_caches:\n"
        f"      - path: {cache}\n"
        f"        uv_binary: {fake_uv}\n"
        "        min_gib: 0\n"
        "        timeout_seconds: 5\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("ONEX_HOST_RETENTION_OVERLAY", raising=False)
    for name, value in installer.environment(install_request, unit).items():
        monkeypatch.setenv(name, value)
    assert main(argv) == 0
    line = json.loads(capsys.readouterr().out)
    receipt = json.loads((state_dir / "last-run.json").read_text(encoding="utf-8"))
    assert line["host"] == host
    assert line["status"] == "ok"
    assert line["dry_run"] is True
    assert line["errors"] == line["refusals"] == []
    assert line["totals"][0]["class_name"] == "uv_cache"
    assert receipt["exit_code"] == 0
    assert receipt["result"] == line

    monkeypatch.setenv("ONEX_HOST_NAME", "undeclared-host")
    assert main(argv) == 2
    line = json.loads(capsys.readouterr().out)
    assert line["host"] == "undeclared-host"
    assert line["status"] == "refused"
    receipt = json.loads((state_dir / "last-run.json").read_text(encoding="utf-8"))
    assert receipt["exit_code"] == 2
    assert receipt["result"] == line
