# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A copied alarm must use the source tree bundled with its scripts."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelRegistryUnitsRequest,
)


def source_tree(root: Path) -> Path:
    """Replay the recorded script/handler version skew without a foreign clone."""
    source = root / "plugin"
    files = {
        "skills/merge-drain/scripts/floor_alarm.py": (
            "import argparse, sys\nfrom pathlib import Path\n"
            "top = Path(__file__).resolve().parents[3]\n"
            "bundled = top / 'src'\n"
            "sys.path.insert(0, str(bundled if bundled.exists() else "
            "Path(__import__('os').environ['OMNI_HOME']) / 'stale' / 'src'))\n"
            "from alarm_support.reader import ledger_text\n"
            "p = argparse.ArgumentParser(description=ledger_text())\n"
            "p.add_argument('--state-dir', required=True)\n"
            "p.add_argument('--ledger', required=True)\n"
            "p.parse_args()\n"
        ),
        "skills/merge-drain/scripts/landing_scorecard.py": "# scorecard\n",
        "skills/merge-drain/scripts/pr_state_local.py": "# watcher reader\n",
        "skills/merge-drain/landing_floors.json": "{}\n",
        "skills/queue-status/scripts/queue_status.py": "# queue reader\n",
        "src/alarm_support/__init__.py": "",
        "src/alarm_support/reader.py": "def ledger_text():\n    return 'bundled-reader'\n",
    }
    for name, text in files.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    stale = root / "registry/stale/src/alarm_support"
    stale.mkdir(parents=True)
    (stale / "__init__.py").write_text("")
    (stale / "reader.py").write_text("# old handler has no ledger_text\n")
    return source


def request(
    root: Path, source: Path, mode: str = "dry-run"
) -> ModelRegistryUnitsRequest:
    return ModelRegistryUnitsRequest(
        unit="floor-alarm",
        mode=mode,
        home=str(root / "home"),
        registry_root=str(root / "registry"),
        source_root=str(source),
    )


def handler(calls: list[list[str]]) -> HandlerRegistryUnits:
    units = load_units()
    units["floor-alarm"]["interpreter_candidates"] = [sys.executable]

    def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[0] == sys.executable:
            return subprocess.run(
                argv,
                env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        if argv[:2] == ["id", "-u"]:
            return subprocess.CompletedProcess(argv, 0, "501\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    return HandlerRegistryUnits(run=run, units=units)


def test_recorded_bundle_uses_matching_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = source_tree(tmp_path)
    monkeypatch.setenv("OMNI_HOME", str(tmp_path / "registry"))
    calls: list[list[str]] = []
    result = handler(calls).handle(request(tmp_path, source))
    assert result.exit_code == 0, result.stderr
    assert any(call[-1] == "--help" for call in calls)
    plist = plistlib.loads(result.rendered.encode())
    assert plist["Label"] == "ai.omninode.floor-alarm"
    assert plist["StartInterval"] == 600
    assert plist["RunAtLoad"] is False
    assert plist["ProgramArguments"][-4:] == [
        "--state-dir",
        str(tmp_path / "registry/.onex_state/floor-alarm"),
        "--ledger",
        str(tmp_path / "registry/docs/tracking/ROLLING_WORK_LEDGER.md"),
    ]
    assert len(result.bundle_sha256) == 64
    assert not (tmp_path / "home").exists()
    assert not any(call[0] == "launchctl" for call in calls)


def test_install_carries_src_and_runs_after_source_removed(tmp_path: Path) -> None:
    source = source_tree(tmp_path)
    calls: list[list[str]] = []
    result = handler(calls).handle(request(tmp_path, source, "install"))
    assert result.exit_code == 0, result.stderr
    installed = tmp_path / "home/.omninode/floor-alarm"
    assert (installed / "src/alarm_support/reader.py").read_bytes() == (
        source / "src/alarm_support/reader.py"
    ).read_bytes()
    source.rename(tmp_path / "source-removed")
    plist = plistlib.loads(result.rendered.encode())
    done = subprocess.run(
        [*plist["ProgramArguments"], "--help"],
        env={**os.environ, **plist["EnvironmentVariables"]},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert done.returncode == 0, done.stderr
    assert "bundled-reader" in done.stdout
    assert any(call[1] == "bootstrap" for call in calls if call[0] == "launchctl")


@pytest.mark.parametrize("defect", ["missing-src", "bad-import"])
def test_failed_smoke_leaves_existing_install_loaded(
    tmp_path: Path, defect: str
) -> None:
    source = source_tree(tmp_path)
    dependency = source / "src/alarm_support/reader.py"
    if defect == "missing-src":
        (source / "src").rename(source / "missing-src")
    else:
        dependency.write_text("# ledger_text disappeared\n")
    installed = tmp_path / "home/.omninode/floor-alarm"
    installed.mkdir(parents=True)
    sentinel = installed / "previous"
    sentinel.write_text("keep\n")
    calls: list[list[str]] = []
    result = handler(calls).handle(request(tmp_path, source, "install"))
    assert result.exit_code != 0
    assert sentinel.read_text() == "keep\n"
    assert not any(call[0] == "launchctl" for call in calls)


def test_bootstrap_failure_restores_previous_bundle_and_plist(tmp_path: Path) -> None:
    source = source_tree(tmp_path)
    installed = tmp_path / "home/.omninode/floor-alarm"
    installed.mkdir(parents=True)
    (installed / "previous").write_text("keep\n")
    plist = tmp_path / "home/Library/LaunchAgents/ai.omninode.floor-alarm.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("previous plist\n")
    calls: list[list[str]] = []
    configured = handler(calls)
    original_run = configured.run
    bootstraps = 0

    def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        nonlocal bootstraps
        if argv[:2] == ["launchctl", "bootstrap"]:
            bootstraps += 1
            if bootstraps == 1:
                return subprocess.CompletedProcess(argv, 5, "", "bootstrap refused")
        return original_run(argv)

    configured.run = run
    result = configured.handle(request(tmp_path, source, "install"))
    assert result.exit_code != 0
    assert "bootstrap refused" in result.stderr
    assert (installed / "previous").read_text() == "keep\n"
    assert plist.read_text() == "previous plist\n"
    assert bootstraps == 2


@pytest.mark.asyncio
async def test_copied_alarm_routes_through_canonical_runtime_bus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.runtime_local_compat import RuntimeLocal

    source = source_tree(tmp_path)
    calls: list[list[str]] = []
    configured = handler(calls)
    original_init = HandlerRegistryUnits.__init__
    monkeypatch.setattr(
        HandlerRegistryUnits,
        "__init__",
        lambda self: original_init(self, run=configured.run, units=configured.units),
    )
    input_path = tmp_path / "request.json"
    input_path.write_text(request(tmp_path, source).model_dump_json())
    runtime = RuntimeLocal(
        workflow_path=Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_host_installer_registry_units/contract.yaml",
        input_path=input_path,
        state_root=tmp_path / "bus",
        backend_overrides={"event_bus": "inmemory"},
        timeout=30,
    )
    await runtime.run_async()
    result = runtime.handler_result
    assert result is not None
    assert result.exit_code == 0, result.stderr
    assert len(result.bundle_sha256) == 64
    assert any(call[-1] == "--help" for call in calls)
