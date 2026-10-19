# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read one host's thermal and load facts and append them to the host's own series.

The CPU number is read the way the remote-lane runner's placement probe reads it (k10temp Tctl, or
coretemp Package id, averaged over two reads one sample apart), so a series row and a placement
refusal describe the same quantity. Nothing here raises on a missing sensor: the row says None and
names why.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from ..models import (
    ModelHostGpuReading,
    ModelHostThermalObserveRequest,
    ModelHostThermalReading,
)

SERIES_FILE = "readings.jsonl"
LATEST_FILE = "latest.json"
_CPU_CHIPS = ("k10temp", "coretemp")
_CPU_LABELS = ("Tctl", "Package id")
_GPU_QUERY = "index,temperature.gpu,utilization.gpu,power.draw"

Runner = Callable[..., subprocess.CompletedProcess[str]]


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _milli(path: Path) -> float | None:
    raw = _read(path)
    try:
        return int(raw) / 1000.0 if raw is not None else None
    except ValueError:
        return None


def read_cpu_temp(hwmon_root: Path) -> float | None:
    """The package temperature in C, or None without a k10temp or coretemp package sensor."""
    for chip in sorted(hwmon_root.glob("hwmon*")):
        if _read(chip / "name") not in _CPU_CHIPS:
            continue
        for label_file in sorted(chip.glob("temp*_label")):
            label = _read(label_file) or ""
            if label.startswith(_CPU_LABELS):
                value = _milli(chip / label_file.name.replace("_label", "_input"))
                if value is not None:
                    return value
    return None


def read_dimm_temp(hwmon_root: Path) -> float | None:
    """The hottest spd5118 DIMM in C."""
    values = [
        value
        for chip in sorted(hwmon_root.glob("hwmon*"))
        if _read(chip / "name") == "spd5118"
        for value in [_milli(chip / "temp1_input")]
        if value is not None
    ]
    return max(values) if values else None


def read_fans(hwmon_root: Path) -> tuple[tuple[str, int], ...]:
    """Every fan that reports a nonzero speed, labelled chip:fan."""
    fans: list[tuple[str, int]] = []
    for chip in sorted(hwmon_root.glob("hwmon*")):
        name = _read(chip / "name") or chip.name
        for fan in sorted(chip.glob("fan*_input")):
            raw = _read(fan)
            if raw and raw.isdigit() and int(raw) > 0:
                fans.append((f"{name}:{fan.name.removesuffix('_input')}", int(raw)))
    return tuple(fans)


def _cpu_times(proc_root: Path) -> tuple[int, int, int] | None:
    """(total, idle, iowait) jiffies from the aggregate cpu line."""
    raw = _read(proc_root / "stat")
    if not raw:
        return None
    parts = raw.splitlines()[0].split()
    if parts[0] != "cpu":
        return None
    try:
        fields = [int(part) for part in parts[1:]]
    except ValueError:
        return None
    if len(fields) < 5:
        return None
    return sum(fields[:8]), fields[3], fields[4]


def _meminfo_gb(proc_root: Path) -> tuple[float | None, float | None]:
    """(MemAvailable, swap in use) in GB."""
    values: dict[str, int] = {}
    for line in (_read(proc_root / "meminfo") or "").splitlines():
        key, _, rest = line.partition(":")
        fields = rest.split()
        if fields and fields[0].isdigit():
            values[key] = int(fields[0])
    available = values.get("MemAvailable")
    swap_total, swap_free = values.get("SwapTotal"), values.get("SwapFree")
    return (
        available / 1024**2 if available is not None else None,
        (swap_total - swap_free) / 1024**2
        if swap_total is not None and swap_free is not None
        else None,
    )


def read_gpus(run: Runner) -> tuple[tuple[ModelHostGpuReading, ...], str | None]:
    """The nvidia-smi GPUs; a host with no nvidia-smi has none, which is not an error."""
    try:
        done = run(
            [
                "nvidia-smi",
                f"--query-gpu={_GPU_QUERY}",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except FileNotFoundError:
        return (), None
    except (subprocess.TimeoutExpired, OSError) as exc:
        return (), f"nvidia-smi: {exc}"
    if done.returncode != 0:
        return (), f"nvidia-smi rc={done.returncode}"
    gpus: list[ModelHostGpuReading] = []
    for row in done.stdout.splitlines():
        cells = [cell.strip() for cell in row.split(",")]
        try:
            power = float(cells[3]) if len(cells) > 3 else None
            gpus.append(
                ModelHostGpuReading(
                    int(cells[0]), float(cells[1]), float(cells[2]), power
                )
            )
        except (ValueError, IndexError):
            continue
    return tuple(gpus), None


class HandlerHostThermalObserve:
    """Observe the host this runs on; hwmon, proc and the clock are injectable for tests."""

    def __init__(
        self,
        hwmon_root: Path = Path("/sys/class/hwmon"),
        proc_root: Path = Path("/proc"),
        sleep: Callable[[float], None] = time.sleep,
        run: Runner = subprocess.run,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._hwmon = hwmon_root
        self._proc = proc_root
        self._sleep = sleep
        self._run = run
        self._now = now

    def observe(
        self, request: ModelHostThermalObserveRequest
    ) -> ModelHostThermalReading:
        errors: list[str] = []
        first_temp = read_cpu_temp(self._hwmon)
        first_cpu = _cpu_times(self._proc)
        self._sleep(request.sample_seconds)
        second_temp = read_cpu_temp(self._hwmon)
        second_cpu = _cpu_times(self._proc)
        temps = [t for t in (first_temp, second_temp) if t is not None]
        if not temps:
            errors.append("no k10temp or coretemp package sensor")
        busy = iowait = None
        if first_cpu and second_cpu and second_cpu[0] > first_cpu[0]:
            total = second_cpu[0] - first_cpu[0]
            busy = 100.0 * (
                1
                - (second_cpu[1] - first_cpu[1] + second_cpu[2] - first_cpu[2]) / total
            )
            iowait = 100.0 * (second_cpu[2] - first_cpu[2]) / total
        else:
            errors.append("cpu times unreadable")
        loads = (_read(self._proc / "loadavg") or "").split()
        load = [float(x) for x in loads[:3]] if len(loads) >= 3 else [None, None, None]
        available, swap = _meminfo_gb(self._proc)
        gpus, gpu_error = read_gpus(self._run)
        if gpu_error:
            errors.append(gpu_error)
        return ModelHostThermalReading(
            observed_at=self._now().strftime("%Y-%m-%dT%H:%M:%SZ"),
            host=request.host,
            cpu_temp_c=round(sum(temps) / len(temps), 1) if temps else None,
            dimm_temp_c=read_dimm_temp(self._hwmon),
            load1=load[0],
            load5=load[1],
            load15=load[2],
            cpu_busy_pct=round(busy, 1) if busy is not None else None,
            cpu_iowait_pct=round(iowait, 1) if iowait is not None else None,
            mem_available_gb=round(available, 1) if available is not None else None,
            swap_used_gb=round(swap, 1) if swap is not None else None,
            gpus=gpus,
            fans_rpm=read_fans(self._hwmon),
            errors=tuple(errors),
        )

    def handle(
        self, request: ModelHostThermalObserveRequest
    ) -> ModelHostThermalReading:
        """Observe, then append the row to the series and replace latest.json atomically."""
        reading = self.observe(request)
        if request.dry_run:
            return reading
        request.state_dir.mkdir(parents=True, exist_ok=True)
        with (request.state_dir / SERIES_FILE).open("a", encoding="utf-8") as series:
            series.write(reading.to_json() + "\n")
        staged = request.state_dir / f"{LATEST_FILE}.{os.getpid()}.tmp"
        staged.write_text(
            json.dumps(json.loads(reading.to_json()), indent=2) + "\n", encoding="utf-8"
        )
        os.replace(staged, request.state_dir / LATEST_FILE)
        return reading
