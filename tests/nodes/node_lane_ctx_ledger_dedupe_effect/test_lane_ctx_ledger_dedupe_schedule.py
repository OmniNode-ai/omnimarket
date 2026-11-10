# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane context ledger dedupe is scheduled on every lab host (OMN-23297).

The schedule is the host installer's ``lane-ctx-ledger-dedupe`` systemd unit; its settings are the node's overlay.
These tests read the declaration, the unit it renders, and one tick of the module the unit runs.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelRegistryUnitsRequest,
)
from omnimarket.nodes.node_lane_ctx_ledger_dedupe_effect import (
    runtime_lane_ctx_ledger_dedupe_tick as tick,
)
from omnimarket.nodes.node_lane_ctx_ledger_dedupe_effect.models.model_lane_ctx_ledger_dedupe import (
    ModelLaneCtxLedgerDedupeOverlay,
)

UNIT = "lane-ctx-ledger-dedupe"
LABEL = "onex-lane-ctx-ledger-dedupe"
NODE = "node_lane_ctx_ledger_dedupe_effect"
ROOT = Path(__file__).resolve().parents[3]
NODE_DIR = ROOT / "src" / "omnimarket" / "nodes" / NODE
HANDLER = "omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units"
LINUX_PYTHON = "/usr/bin/python3.12"
ROLL = "ROLLING_WORK_LEDGER_2026-08-29-split.md"


def _git(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        env=scrub_git_location_env(os.environ),
        capture_output=True,
        text=True,
        check=True,
    )


def _checkout(path: Path, entry: str) -> str:
    path.mkdir(parents=True)
    _git(["git", "-C", str(path), "init", "-q", "-b", "branch"])
    (path / entry).parent.mkdir(parents=True)
    (path / entry).write_text("print('tick')\n", encoding="utf-8")
    _git(["git", "-C", str(path), "add", "-A"])
    _git(
        [
            *["git", "-C", str(path), "-c", "user.name=t"],
            *["-c", "user.email=t@example.com", "commit", "-q", "-m", "x"],
        ]
    )
    return _git(["git", "-C", str(path), "rev-parse", "HEAD"]).stdout.strip()


def _recorder(argv: list[str]) -> subprocess.CompletedProcess[str]:
    if argv[0] in ("git", "id"):
        return subprocess.run(
            argv,
            env=scrub_git_location_env(os.environ),
            capture_output=True,
            text=True,
            check=False,
        )
    return subprocess.CompletedProcess(argv, 0, "", "")


@pytest.fixture
def linux_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        f"{HANDLER}.interpreter_present", lambda path: path == LINUX_PYTHON
    )
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )


def _render(tmp_path: Path, units: dict[str, Any] | None = None) -> Any:
    declared = load_units() if units is None else units
    entry = declared[UNIT]["entry_file"]
    path = tmp_path / "branch"
    revision = _checkout(path, entry)
    return HandlerRegistryUnits(_recorder, units=declared).handle(
        ModelRegistryUnitsRequest(
            unit=UNIT,
            home=str(tmp_path / "home"),
            registry_root=str(tmp_path / "omni"),
            project_path=str(path),
            expected_revision=revision,
            path_overrides={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "overlays")},
        )
    )


# --- the schedule is declared -----------------------------------------------------------------


def test_the_schedule_is_declared_for_a_linux_host_with_a_bounded_run() -> None:
    declared = load_units()[UNIT]
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    assert declared["platform"] == "systemd"
    assert declared["kind"] == "registry_module_tick"
    assert declared["schedule"]["start_interval"] > 0
    # The unit ends a run at the bound the node declares, and before the next tick is due.
    limit = declared["schedule"]["run_timeout_s"]
    assert limit * 1000 == contract["descriptor"]["timeout_ms"]
    assert limit < declared["schedule"]["start_interval"]
    assert (ROOT / declared["entry_file"]).is_file()
    assert declared["module"] == (
        declared["entry_file"]
        .removeprefix("src/")
        .removesuffix(".py")
        .replace("/", ".")
    )
    assert declared["environment_path_overrides"] == ["ONEX_SKILL_OVERLAY_ROOTS"]


def test_the_unit_holds_none_of_the_nodes_settings() -> None:
    text = json.dumps(load_units()[UNIT])
    for setting in ("runs_root", "store_dir", "settle_seconds"):
        assert setting not in text


def test_the_rendered_unit_runs_the_tick_module_on_the_declared_interval(
    tmp_path: Path, linux_host: None
) -> None:
    result = _render(tmp_path)
    assert result.status == "success", result.stderr
    exec_line = next(
        line for line in result.rendered.splitlines() if line.startswith("ExecStart=")
    )
    assert (
        f'"-m" "omnimarket.nodes.{NODE}.runtime_lane_ctx_ledger_dedupe_tick"'
        in exec_line
    )
    assert '"--omni-home"' in exec_line
    assert '"--state-dir"' in exec_line
    assert f'"ONEX_SKILL_OVERLAY_ROOTS={tmp_path}/overlays"' in result.rendered
    assert "TimeoutStartSec=1500" in result.rendered
    assert "OnUnitActiveSec=1800" in result.rendered
    assert f"Unit={LABEL}.service" in result.rendered


def test_the_rendered_interval_follows_the_declaration(
    tmp_path: Path, linux_host: None
) -> None:
    # Control: the interval asserted above is the declared one, not a default of the template.
    units = load_units()
    units[UNIT] = {
        **units[UNIT],
        "schedule": {**units[UNIT]["schedule"], "start_interval": 600},
    }
    result = _render(tmp_path, units)
    assert result.status == "success", result.stderr
    assert "OnUnitActiveSec=600" in result.rendered
    assert "OnUnitActiveSec=1800" not in result.rendered


# --- one tick ---------------------------------------------------------------------------------


def _overlay(tmp_path: Path, runs: Path, store: Path, settle: int = 0) -> Path:
    home = tmp_path / "overlays" / NODE
    home.mkdir(parents=True)
    (home / "overlay.yaml").write_text(
        f"runs_root: {runs}\nstore_dir: {store}\nsettle_seconds: {settle}\n",
        encoding="utf-8",
    )
    return tmp_path / "overlays"


def _context(runs: Path, run: str, data: bytes) -> Path:
    archive = runs / f"{run}.ctx" / "ledger" / "archive"
    archive.mkdir(parents=True)
    path = archive / ROLL
    path.write_bytes(data)
    path.chmod(0o444)
    os.utime(path, (1.0, 1.0))
    return path


def test_a_tick_links_two_lanes_and_writes_its_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs, store, state = (
        tmp_path / "runs",
        tmp_path / "runs" / ".store",
        tmp_path / "st",
    )
    runs.mkdir()
    a, b = _context(runs, "lane-a", b"roll"), _context(runs, "lane-b", b"roll")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(_overlay(tmp_path, runs, store)))
    monkeypatch.delenv("KAFKA_BROKERS", raising=False)

    assert a.stat().st_ino != b.stat().st_ino
    assert tick.main(["--omni-home", str(tmp_path), "--state-dir", str(state)]) == 0

    assert a.stat().st_ino == b.stat().st_ino == (store / ROLL).stat().st_ino
    evidence = json.loads((state / "last-run.json").read_text(encoding="utf-8"))
    assert evidence["exit_code"] == 0
    assert evidence["result"]["linked"] == 1
    assert evidence["result"]["contexts"] == 2


def test_a_tick_with_no_overlay_links_nothing_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    state = tmp_path / "st"
    assert tick.main(["--omni-home", str(tmp_path), "--state-dir", str(state)]) == 3
    evidence = json.loads((state / "last-run.json").read_text(encoding="utf-8"))
    assert evidence["exit_code"] == 3
    assert "indeterminate" in evidence


def test_a_tick_that_refuses_a_roll_exits_one_and_records_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs, store, state = (
        tmp_path / "runs",
        tmp_path / "runs" / ".store",
        tmp_path / "st",
    )
    runs.mkdir()
    _context(runs, "lane-a", b"roll")
    _context(runs, "lane-b", b"roll")
    store.mkdir()
    store.chmod(0o555)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(_overlay(tmp_path, runs, store)))
    monkeypatch.delenv("KAFKA_BROKERS", raising=False)
    try:
        code = tick.main(["--omni-home", str(tmp_path), "--state-dir", str(state)])
    finally:
        store.chmod(0o755)
    if os.geteuid() == 0:
        pytest.skip("root writes into a read-only directory")
    assert code == 1
    evidence = json.loads((state / "last-run.json").read_text(encoding="utf-8"))
    assert evidence["exit_code"] == 1
    assert evidence["result"]["errors"]


# --- the overlay model ------------------------------------------------------------------------


def test_the_overlay_expands_home_and_names_no_home_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", "/h")
    overlay = ModelLaneCtxLedgerDedupeOverlay.model_validate(
        {
            "runs_root": "$HOME/lab-run/runs",
            "store_dir": "~/lab-run/runs/.ledger-archive-store",
            "settle_seconds": 300,
        }
    )
    assert overlay.runs_root == "/h/lab-run/runs"
    assert overlay.store_dir == "/h/lab-run/runs/.ledger-archive-store"


@pytest.mark.parametrize("bad", ["relative/runs", "$UNSET_ZZ_VAR/runs"])
def test_the_overlay_refuses_a_path_that_is_not_absolute_once_expanded(
    bad: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("UNSET_ZZ_VAR", raising=False)
    with pytest.raises(ValueError, match="absolute"):
        ModelLaneCtxLedgerDedupeOverlay.model_validate(
            {"runs_root": bad, "store_dir": "/s", "settle_seconds": 1}
        )
