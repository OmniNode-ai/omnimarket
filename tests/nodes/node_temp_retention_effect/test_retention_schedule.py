# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node owns its timer and its declared rows; the facts below are fixtures or the packaged overlay."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_temp_retention_effect import __main__ as node_main
from omnimarket.nodes.node_temp_retention_effect.handlers.handler_temp_retention import (
    NODE_DIR,
    HandlerTempRetention,
    ProcessReferences,
    RefusalError,
    host_row,
    load_bounds,
    load_overlay,
    load_schedule,
)
from omnimarket.nodes.node_temp_retention_effect.models.model_temp_retention import (
    ModelRetentionWindows,
    ModelTempRetentionHost,
    ModelTempRetentionOverlay,
    ModelTempRoot,
)
from omnimarket.nodes.node_temp_retention_effect.models.model_temp_retention_request import (
    ModelTempRetentionRequest,
)
from omnimarket.nodes.node_temp_retention_effect.retention_schedule import (
    NODE_MODULE,
    RetentionScheduler,
    render_units,
)

pytestmark = pytest.mark.unit

TIMER = "onex-temp-retention.timer"
SERVICE = "onex-temp-retention.service"


def row(name: str = "pool-a") -> ModelTempRetentionHost:
    return ModelTempRetentionHost(
        name=name,
        scheduler="systemd-user",
        windows=ModelRetentionWindows(
            retain_hours=12, pressure_percent=80, pressure_retain_hours=2
        ),
        temp_roots=(ModelTempRoot(path="/srv/fixture/runs/.tmp"),),
    )


class Systemctl:
    """A fake command runner that records each command and answers like systemctl."""

    def __init__(self, *, listed: bool = True, import_ok: bool = True) -> None:
        self.calls: list[list[str]] = []
        self.listed = listed
        self.import_ok = import_ok

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(command))
        if command[0] == "env":
            return subprocess.CompletedProcess(
                list(command), 0 if self.import_ok else 1, "", "no module"
            )
        if command[:3] == ["systemctl", "--user", "list-timers"]:
            out = f"NEXT LEFT\n{TIMER}\n" if self.listed else "0 timers listed.\n"
            return subprocess.CompletedProcess(list(command), 0, out, "")
        return subprocess.CompletedProcess(list(command), 0, "", "")


def scheduler(tmp_path: Path, runner: Systemctl) -> RetentionScheduler:
    return RetentionScheduler(
        schedule=load_schedule(), rows=[row()], home=tmp_path / "home", run=runner
    )


def fixture_python(tmp_path: Path) -> Path:
    python = tmp_path / "python"
    python.write_text("")
    return python


def install_args(tmp_path: Path) -> dict[str, Path]:
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text("hosts: []")
    return {
        "python": fixture_python(tmp_path),
        "source_root": tmp_path / "checkout",
        "overlay_file": overlay,
    }


def test_units_carry_the_contract_cadence_and_the_overlay_row(tmp_path: Path) -> None:
    service, timer = render_units(
        load_schedule(),
        row(),
        python=Path("/srv/fixture/venv/bin/python"),
        source_root=Path("/srv/co"),
        overlay_file=Path("/srv/co/o.yaml"),
        home=Path("/home/x"),
    )
    assert (
        f'ExecStart="/srv/fixture/venv/bin/python" -m {NODE_MODULE} run --host "pool-a"'
        in service
    )
    assert 'Environment="PYTHONPATH=/srv/co/src"' in service
    assert 'Environment="ONEX_TEMP_RETENTION_OVERLAY=/srv/co/o.yaml"' in service
    assert "TimeoutStartSec=900" in service
    assert "OnUnitActiveSec=300s" in timer
    assert f"Unit={SERVICE}" in timer


def test_a_relative_path_cannot_be_rendered_into_a_unit() -> None:
    with pytest.raises(ValueError, match="absolute"):
        render_units(
            load_schedule(),
            row(),
            python=Path("/srv/fixture/venv/bin/python"),
            source_root=Path("checkout"),
            overlay_file=Path("/srv/co/o.yaml"),
            home=Path("/home/x"),
        )


def test_install_writes_both_units_then_enables_the_timer_and_a_second_install_changes_nothing(
    tmp_path: Path,
) -> None:
    runner = Systemctl()
    sched = RetentionScheduler(
        schedule=load_schedule(),
        rows=[row()],
        home=tmp_path / "home",
        run=runner,
    )
    first = sched.install("pool-a", **install_args(tmp_path))
    assert first.exit_code == 0, first.stderr
    assert (tmp_path / "home/.config/systemd/user" / SERVICE).is_file()
    assert (tmp_path / "home/.config/systemd/user" / TIMER).is_file()
    assert ["systemctl", "--user", "enable", "--now", TIMER] in runner.calls
    second = sched.install("pool-a", **install_args(tmp_path))
    assert f"{SERVICE} unchanged" in second.stdout


def test_install_refuses_a_host_the_overlay_does_not_declare(tmp_path: Path) -> None:
    result = scheduler(tmp_path, Systemctl()).install(
        "pool-z", **install_args(tmp_path)
    )
    assert result.exit_code == 1
    assert "not in the node_temp_retention_effect overlay" in result.stderr


def test_install_refuses_when_the_node_cannot_be_imported(tmp_path: Path) -> None:
    sched = RetentionScheduler(
        schedule=load_schedule(),
        rows=[row()],
        home=tmp_path / "home",
        run=Systemctl(import_ok=False),
    )
    result = sched.install("pool-a", **install_args(tmp_path))
    assert result.exit_code == 1
    assert "cannot import" in result.stderr
    assert not (tmp_path / "home/.config/systemd/user" / TIMER).exists()


def test_install_fails_when_systemd_lists_no_schedule(tmp_path: Path) -> None:
    sched = RetentionScheduler(
        schedule=load_schedule(),
        rows=[row()],
        home=tmp_path / "home",
        run=Systemctl(listed=False),
    )
    result = sched.install("pool-a", **install_args(tmp_path))
    assert result.exit_code == 1
    assert "lists no schedule" in result.stderr


def test_dry_run_installs_nothing(tmp_path: Path) -> None:
    runner = Systemctl()
    result = scheduler(tmp_path, runner).install(
        "pool-a", dry_run=True, **install_args(tmp_path)
    )
    assert result.exit_code == 0
    assert runner.calls == []
    assert not (tmp_path / "home").exists()


def test_uninstall_removes_the_units(tmp_path: Path) -> None:
    runner = Systemctl()
    sched = RetentionScheduler(
        schedule=load_schedule(),
        rows=[row()],
        home=tmp_path / "home",
        run=runner,
    )
    sched.install("pool-a", **install_args(tmp_path))
    assert sched.uninstall("pool-a").exit_code == 0
    assert not (tmp_path / "home/.config/systemd/user" / TIMER).exists()


def test_the_packaged_overlay_validates_and_declares_both_pool_hosts() -> None:
    overlay = load_overlay(str(NODE_DIR / "overlay.yaml"))
    assert [h.name for h in overlay.hosts] == ["h051", "h053"]
    bounds = load_bounds()
    for declared in overlay.hosts:
        for root in declared.temp_roots:
            assert Path(root.path).name in bounds.temp_root_leaf_names
        for store in declared.store_ceilings:
            assert Path(store.path).name == bounds.store_leaf_names[store.kind]
        assert declared.windows.retain_hours == 12
        assert declared.windows.pressure_retain_hours == 2
    # the recorded gap: the lanes' TMPDIR and the package store sit beside the runs on both hosts
    for declared in overlay.hosts:
        assert "/data/lab-run/runs/.tmp" in [r.path for r in declared.temp_roots]
        assert "/data/lab-run/runs/.pnpm-store" in [
            s.path for s in declared.store_ceilings
        ]


def test_the_synthetic_example_overlay_validates() -> None:
    document = yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())
    assert ModelTempRetentionOverlay.model_validate(document).hosts[0].name == "host-a"


def test_a_duplicate_host_row_is_refused() -> None:
    overlay = ModelTempRetentionOverlay(hosts=(row(), row()))
    with pytest.raises(RefusalError):
        host_row(overlay, "pool-a")


def test_no_overlay_source_refuses_with_the_setting_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ONEX_TEMP_RETENTION_OVERLAY", raising=False)
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    with pytest.raises(NodeOverlayError):
        load_overlay()


def test_the_node_names_no_host_path_or_number_of_its_own() -> None:
    sources = [
        NODE_DIR / "handlers" / "handler_temp_retention.py",
        NODE_DIR / "retention_schedule.py",
        NODE_DIR / "runtime_temp_retention.py",
    ]
    forbidden = ("h051", "h053", "/data/", "/tmp", "lab-run")
    for source in sources:
        text = source.read_text()
        for word in forbidden:
            assert word not in text, f"{source.name} names {word}"


def write_overlay(tmp_path: Path) -> tuple[Path, Path]:
    runs = tmp_path / "runs" / ".tmp"
    runs.mkdir(parents=True)
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        yaml.safe_dump(
            {
                "hosts": [
                    {
                        "name": "pool-a",
                        "scheduler": "systemd-user",
                        "windows": {
                            "retain_hours": 12,
                            "pressure_percent": 80,
                            "pressure_retain_hours": 2,
                        },
                        "temp_roots": [{"path": str(runs)}],
                    }
                ]
            }
        )
    )
    return runs, overlay


def test_handler_reads_the_overlay_row_and_reports_one_pass(tmp_path: Path) -> None:
    _runs, overlay = write_overlay(tmp_path)
    handler = HandlerTempRetention(references=ProcessReferences)
    result = handler.handle(
        ModelTempRetentionRequest(
            host_name="pool-a", overlay_file=str(overlay), dry_run=True
        )
    )
    assert result.host == "pool-a"
    assert result.dry_run
    assert result.removed_count == 0
    assert not result.errors
    assert json.loads(result.model_dump_json())["host"] == "pool-a"


def test_main_run_for_an_undeclared_host_is_indeterminate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _runs, overlay = write_overlay(tmp_path)
    assert node_main.main(["run", "--host", "pool-z", "--overlay", str(overlay)]) == 3
    assert "not in the node_temp_retention_effect overlay" in capsys.readouterr().err
