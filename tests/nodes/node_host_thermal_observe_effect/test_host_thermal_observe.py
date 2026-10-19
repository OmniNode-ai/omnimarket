# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host thermal observation: a fake hwmon and proc tree, the series file, the report."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.nodes.node_host_thermal_observe_effect import handlers
from omnimarket.nodes.node_host_thermal_observe_effect.handlers import (
    handler_host_thermal_cli as cli,
)
from omnimarket.nodes.node_host_thermal_observe_effect.handlers import (
    handler_host_thermal_report as summary,
)
from omnimarket.nodes.node_host_thermal_observe_effect.models import (
    ModelHostThermalObserveRequest,
)

HandlerHostThermalObserve = handlers.HandlerHostThermalObserve
report = summary.report
main = cli.main


def _chip(root: Path, n: int, name: str, files: dict[str, str]) -> None:
    chip = root / f"hwmon{n}"
    chip.mkdir(parents=True)
    (chip / "name").write_text(name + "\n")
    for key, value in files.items():
        (chip / key).write_text(value + "\n")


def _host(tmp_path: Path, tctl_milli: int = 91500) -> tuple[Path, Path]:
    hwmon, proc = tmp_path / "hwmon", tmp_path / "proc"
    proc.mkdir()
    _chip(hwmon, 0, "k10temp", {"temp1_label": "Tctl", "temp1_input": str(tctl_milli)})
    _chip(hwmon, 1, "spd5118", {"temp1_input": "41500"})
    _chip(hwmon, 2, "spd5118", {"temp1_input": "55000"})
    _chip(hwmon, 3, "nct6686", {"fan1_input": "0", "fan2_input": "800"})
    (proc / "loadavg").write_text("3.80 5.10 9.00 2/1000 123\n")
    (proc / "stat").write_text("cpu  100 0 100 700 100 0 0 0 0 0\n")
    (proc / "meminfo").write_text(
        "MemAvailable:   33554432 kB\nSwapTotal:   33554432 kB\nSwapFree:    16777216 kB\n"
    )
    return hwmon, proc


def _handler(
    hwmon: Path, proc: Path, smi: str | None = None
) -> HandlerHostThermalObserve:
    def sleep(_seconds: float) -> None:
        # one second later: 80 of the 100 new jiffies are busy, 10 are iowait
        (proc / "stat").write_text("cpu  150 0 130 770 110 0 0 0 0 0\n")

    def run(argv: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
        if smi is None:
            raise FileNotFoundError(argv[0])
        return subprocess.CompletedProcess(argv, 0, smi, "")

    return HandlerHostThermalObserve(
        hwmon, proc, sleep, run, lambda: datetime(2026, 10, 9, 16, 30, tzinfo=UTC)
    )


def test_reading_matches_what_the_placement_probe_reads(tmp_path: Path) -> None:
    hwmon, proc = _host(tmp_path)
    reading = _handler(hwmon, proc, "0, 32, 7, 21.25\n").observe(
        ModelHostThermalObserveRequest(host="labhost-a", state_dir=tmp_path / "s")
    )
    assert reading.cpu_temp_c == 91.5
    assert reading.dimm_temp_c == 55.0
    assert (reading.load1, reading.load5, reading.load15) == (3.8, 5.1, 9.0)
    assert reading.cpu_busy_pct == 50.0
    assert reading.cpu_iowait_pct == 6.2
    assert reading.mem_available_gb == 32.0
    assert reading.swap_used_gb == 16.0
    assert [(g.index, g.temp_c, g.util_pct, g.power_w) for g in reading.gpus] == [
        (0, 32.0, 7.0, 21.25)
    ]
    assert reading.fans_rpm == (("nct6686:fan2", 800),)
    assert reading.errors == ()


def test_a_host_with_no_sensor_or_gpu_reports_none_not_zero(tmp_path: Path) -> None:
    hwmon, proc = tmp_path / "hwmon", tmp_path / "proc"
    hwmon.mkdir()
    proc.mkdir()
    reading = _handler(hwmon, proc).observe(
        ModelHostThermalObserveRequest(host="mac", state_dir=tmp_path / "s")
    )
    assert reading.cpu_temp_c is None
    assert reading.dimm_temp_c is None
    assert reading.gpus == ()
    assert "no k10temp or coretemp package sensor" in reading.errors


def test_handle_appends_one_line_per_run_and_replaces_latest(tmp_path: Path) -> None:
    hwmon, proc = _host(tmp_path)
    state = tmp_path / "state"
    handler = _handler(hwmon, proc)
    for _ in range(2):
        handler.handle(
            ModelHostThermalObserveRequest(host="labhost-a", state_dir=state)
        )
    lines = (state / "readings.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["cpu_temp_c"] == 91.5
    assert json.loads((state / "latest.json").read_text())["host"] == "labhost-a"
    assert list(state.glob("*.tmp")) == []


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    hwmon, proc = _host(tmp_path)
    state = tmp_path / "state"
    _handler(hwmon, proc).handle(
        ModelHostThermalObserveRequest(host="h", state_dir=state, dry_run=True)
    )
    assert not state.exists()


def test_report_groups_by_load_per_core_and_counts_readings_over_the_bar(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    rows = [
        ("2026-10-09T16:00:00Z", 92.0, 40.0),
        ("2026-10-09T16:05:00Z", 90.0, 36.0),
        ("2026-10-09T17:00:00Z", 70.0, 0.5),
        ("2026-10-09T17:05:00Z", 68.0, 1.0),
    ]
    (state / "readings.jsonl").write_text(
        "".join(
            json.dumps({"observed_at": at, "cpu_temp_c": temp, "load1": load}) + "\n"
            for at, temp, load in rows
        )
        + "not json\n"
    )
    text = report(state, cores=32)
    assert "readings=4" in text
    assert "over_bar=2/4" in text
    assert "idle (<0.15/core): n=2 min=68.0 avg=69.0 max=70.0" in text
    assert "loaded: n=2 min=90.0 avg=91.0 max=92.0" in text
    assert "2026-10-09T17Z: n=2" in text
    assert "readings=2" in report(state, cores=32, since="2026-10-09T17:00:00Z")
    assert report(tmp_path / "none", cores=32) == "no readings"


def test_cli_needs_a_state_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONEX_HOST_THERMAL_STATE_DIR", raising=False)
    assert main(["observe"]) == 2
    monkeypatch.setenv("ONEX_HOST_THERMAL_STATE_DIR", str(tmp_path / "s"))
    assert main(["report"]) == 0
