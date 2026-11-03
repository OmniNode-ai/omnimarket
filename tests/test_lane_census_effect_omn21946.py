# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane census runs from a systemd timer on a Docker Engine host, never from launchd.

The Mac job the census used to run as aborted with "docker inventory could not be observed"
(collector exit 4) because Docker Desktop was not running there. The node declares the census
for a Docker Engine host only: the contract has no launchd scheduler, a host row that is not a
Docker Engine does not validate, and a run counts as having observed the inventory only when it
left a snapshot of its own. The tests name no real host: the hosts here are fixtures.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.model_lane_census import (
    ModelLaneCensusCommand,
    ModelLaneCensusHostRow,
    ModelLaneCensusScheduleOverlay,
)
from omnimarket.models.overlay_keys import ModelLabHostRow, ModelLabHostsTable
from omnimarket.nodes.node_lane_census_effect import __main__ as node_main
from omnimarket.nodes.node_lane_census_effect.handlers import (
    adapter_lane_census_schedule as schedule_module,
)
from omnimarket.nodes.node_lane_census_effect.handlers.adapter_lane_census_schedule import (
    LaneCensusScheduler,
    load_schedule,
    render_units,
    schedule_gaps,
)
from omnimarket.nodes.node_lane_census_effect.handlers.handler_lane_census_effect import (
    HandlerLaneCensusEffect,
    Runner,
)

pytestmark = pytest.mark.unit

NODE_DIR = Path(schedule_module.__file__).resolve().parents[1]
TIMER = "onex-lane-census.timer"


def hosts(*names: str) -> ModelLaneCensusScheduleOverlay:
    return ModelLaneCensusScheduleOverlay(
        hosts=tuple(
            ModelLaneCensusHostRow(name=name, scheduler="systemd-user", docker="engine")
            for name in names
        )
    )


def command(tmp_path: Path, *, memory: bool = False) -> ModelLaneCensusCommand:
    return ModelLaneCensusCommand(
        collector="collector.sh",
        snapshot=str(tmp_path / "state" / "census-snapshot.json"),
        memory=memory,
    )


def collector(
    exit_code: int, *, writes: Path | None = None, stderr: str = ""
) -> Runner:
    """A collector stand-in: exits ``exit_code`` and writes the snapshot when ``writes`` is given."""

    def run(argv: Sequence[str], timeout_s: int) -> subprocess.CompletedProcess[str]:
        assert argv[0] == "bash"
        assert "--snapshot" in argv
        if writes is not None:
            writes.write_text('{"emitted_at": "now"}', encoding="utf-8")
        return subprocess.CompletedProcess(list(argv), exit_code, "", stderr)

    return run


def test_lane_census_contract_declares_systemd_user_and_no_launchd() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    schedule = load_schedule()
    assert set(schedule.schedulers) == {"systemd-user"}
    assert contract["metadata"]["execution_locus"] == "host-local"
    assert contract["metadata"]["requires_docker_engine"] is True
    # Positive control: the scan finds the word where it is written.
    assert "launchd" in "a launchd agent"
    scheduler_text = yaml.safe_dump(contract["schedule"]).lower()
    assert "launchd" not in scheduler_text
    assert "plist" not in scheduler_text


def test_lane_census_node_ships_no_launchd_declaration() -> None:
    files = [
        p for p in NODE_DIR.rglob("*") if p.is_file() and "__pycache__" not in p.parts
    ]
    assert files, "positive control: the node directory holds files"
    assert not [p for p in files if p.suffix in {".plist", ".tmpl"}]
    # The word is allowed in prose that says why it is absent; no unit text is rendered with it.
    service, timer = render_units(
        load_schedule(),
        omni_root=Path("/srv/omni"),
        venv=Path("/srv/omni/.onex-dispatch-venv"),
        home=Path("/home/u"),
    )
    assert "launchd" not in (service + timer).lower()
    assert "<plist" not in service


def test_lane_census_host_row_must_be_a_docker_engine_on_a_systemd_timer() -> None:
    with pytest.raises(ValidationError):
        ModelLaneCensusHostRow.model_validate(
            {"name": "h", "scheduler": "systemd-user", "docker": "desktop"}
        )
    with pytest.raises(ValidationError):
        ModelLaneCensusHostRow.model_validate(
            {"name": "h", "scheduler": "launchd", "docker": "engine"}
        )
    with pytest.raises(ValidationError):
        ModelLaneCensusHostRow.model_validate(
            {"name": "h", "scheduler": "systemd-user"}
        )
    ok = ModelLaneCensusHostRow.model_validate({"name": "h", "docker": "engine"})
    assert ok.scheduler == "systemd-user"


def test_lane_census_overlay_example_validates_and_has_no_gap() -> None:
    document = yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text("utf-8"))
    overlay = ModelLaneCensusScheduleOverlay.model_validate(document)
    assert schedule_gaps(overlay) == ()


def test_lane_census_duplicate_host_is_a_gap_naming_it() -> None:
    gaps = schedule_gaps(hosts("host-a", "host-b", "host-a"))
    assert gaps == ("host-a: declared more than once",)
    result = LaneCensusScheduler(
        schedule=load_schedule(),
        overlay=hosts("host-a", "host-a"),
        home=Path("/home/u"),
    ).check()
    assert result.exit_code == 1
    assert "GAP host-a" in result.stderr


def lab_table(*rows: ModelLabHostRow) -> ModelLabHostsTable:
    return ModelLabHostsTable(hosts=rows)


def lab_row(
    name: str, *, docker: bool | None = None, available: bool = True
) -> ModelLabHostRow:
    return ModelLabHostRow(name=name, target=name, docker=docker, available=available)


def test_lane_census_lab_table_refuses_a_docker_false_host_by_name() -> None:
    table = lab_table(
        lab_row("host-a", docker=True),
        lab_row("host-mac", docker=False),
        lab_row("host-down", available=False),
        lab_row("host-default"),
    )
    overlay = hosts("host-a", "host-mac", "host-down", "host-none", "host-default")
    gaps = schedule_gaps(overlay, table)
    assert gaps == (
        "host-down: the lab host table marks the host unavailable",
        "host-mac: the lab host table says docker: false, so the host takes no Docker work",
        "host-none: names a host the lab host table does not have",
    )
    # Positive control: the same overlay without the table reads clean, and a Docker host passes.
    assert schedule_gaps(overlay) == ()
    assert schedule_gaps(hosts("host-a", "host-default"), table) == ()
    result = LaneCensusScheduler(
        schedule=load_schedule(), overlay=overlay, home=Path("/home/u")
    ).check(table)
    assert result.exit_code == 1
    assert "GAP host-mac" in result.stderr


def test_lane_census_rendered_units_run_the_node_after_docker() -> None:
    schedule = load_schedule()
    service, timer = render_units(
        schedule,
        omni_root=Path("/srv/omni"),
        venv=Path("/srv/omni/.onex-dispatch-venv"),
        home=Path("/home/u"),
        config_root=Path("/srv/config"),
    )
    assert "After=docker.service" in service
    assert (
        '"/srv/omni/.onex-dispatch-venv/bin/python" -m '
        "omnimarket.nodes.node_lane_census_effect"
    ) in service
    assert "--snapshot" in service
    assert 'Environment="ONEX_WORKSPACE_CONFIG_ROOT=/srv/config"' in service
    assert "OnCalendar=hourly" in timer
    assert "Unit=onex-lane-census.service" in timer


def test_lane_census_install_writes_units_and_enables_the_timer(tmp_path: Path) -> None:
    omni = tmp_path / "omni"
    python = omni / ".onex-dispatch-venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    calls: list[list[str]] = []

    def run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    scheduler = LaneCensusScheduler(
        schedule=load_schedule(),
        overlay=hosts("host-a"),
        home=tmp_path / "home",
        run=run,
    )
    refused = scheduler.install("host-z", omni_root=omni)
    assert refused.exit_code == 1
    assert "host-z" in refused.stderr
    assert calls == []
    done = scheduler.install("host-a", omni_root=omni)
    assert done.exit_code == 0
    unit_dir = tmp_path / "home" / ".config/systemd/user"
    assert (unit_dir / TIMER).is_file()
    assert (unit_dir / "onex-lane-census.service").is_file()
    assert ["systemctl", "--user", "enable", "--now", TIMER] in calls


def test_lane_census_run_counts_observed_only_with_a_fresh_snapshot(
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / "state" / "census-snapshot.json"
    request = command(tmp_path)
    clean = HandlerLaneCensusEffect(run=collector(0, writes=snapshot)).handle(request)
    assert (clean.outcome, clean.snapshot_fresh, clean.docker_observed) == (
        "clean",
        True,
        True,
    )
    drift = HandlerLaneCensusEffect(run=collector(30, writes=snapshot)).handle(request)
    assert (drift.outcome, drift.docker_observed) == ("drift", True)


def test_lane_census_unobservable_inventory_is_not_observed(tmp_path: Path) -> None:
    snapshot = tmp_path / "state" / "census-snapshot.json"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text("{}", encoding="utf-8")
    os.utime(snapshot, (1, 1))  # a snapshot from an earlier tick
    result = HandlerLaneCensusEffect(
        run=collector(
            4, stderr="ABORT: docker inventory could not be observed (exit 4)"
        )
    ).handle(command(tmp_path))
    assert result.exit_code == 4
    assert result.outcome == "inventory-unobservable"
    assert result.snapshot_fresh is False
    assert result.docker_observed is False
    assert result.diagnostics == (
        "ABORT: docker inventory could not be observed (exit 4)",
    )


def test_lane_census_exit_zero_with_a_stale_snapshot_observed_nothing(
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / "state" / "census-snapshot.json"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text("{}", encoding="utf-8")
    os.utime(snapshot, (1, 1))
    result = HandlerLaneCensusEffect(run=collector(0)).handle(command(tmp_path))
    assert result.outcome == "clean"
    assert result.snapshot_fresh is False
    assert result.docker_observed is False


def test_lane_census_host_undeclared_and_unknown_codes(tmp_path: Path) -> None:
    snapshot = tmp_path / "state" / "census-snapshot.json"
    undeclared = HandlerLaneCensusEffect(run=collector(5, writes=snapshot)).handle(
        command(tmp_path)
    )
    assert undeclared.outcome == "host-undeclared"
    assert undeclared.docker_observed is False
    unknown = HandlerLaneCensusEffect(run=collector(99, writes=snapshot)).handle(
        command(tmp_path)
    )
    assert unknown.outcome == "collector-error"


def test_lane_census_memory_flag_reaches_the_collector(tmp_path: Path) -> None:
    seen: list[list[str]] = []

    def run(argv: Sequence[str], timeout_s: int) -> subprocess.CompletedProcess[str]:
        seen.append(list(argv))
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    HandlerLaneCensusEffect(run=run).handle(command(tmp_path))
    HandlerLaneCensusEffect(run=run).handle(command(tmp_path, memory=True))
    assert "--memory" not in seen[0]
    assert seen[1][-1] == "--memory"


def test_lane_census_main_refuses_a_zero_that_wrote_no_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The entry point turns "exit 0, nothing observed" into exit 4, as the Mac job's abort was."""
    root = tmp_path / "omni"
    collector_path = root / "omnibase_infra" / "scripts" / "lane-census-check.sh"
    collector_path.parent.mkdir(parents=True)
    snapshot = tmp_path / "snap" / "census-snapshot.json"
    collector_path.write_text("exit 0\n", encoding="utf-8")
    assert node_main.main(["--omni-home", str(root), "--snapshot", str(snapshot)]) == 4
    collector_path.write_text(
        f'printf "{{}}" > "{snapshot}"\nexit 0\n', encoding="utf-8"
    )
    # The stand-in ignores its arguments and writes the snapshot, as the real collector does.
    assert node_main.main(["--omni-home", str(root), "--snapshot", str(snapshot)]) == 0
    monkeypatch.delenv("OMNI_HOME", raising=False)
    assert node_main.main([]) == 3
