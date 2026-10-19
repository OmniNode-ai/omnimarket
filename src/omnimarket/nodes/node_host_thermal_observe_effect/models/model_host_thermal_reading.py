# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One host thermal reading: what the placement bar judges, plus the load that explains it."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class ModelHostGpuReading:
    """One GPU as nvidia-smi reports it."""

    index: int
    temp_c: float
    util_pct: float
    power_w: float | None = None


@dataclass(frozen=True, slots=True)
class ModelHostThermalReading:
    """None means the host has no such sensor, never zero.

    cpu_temp_c is the number the remote-lane runner's admission bar reads: k10temp Tctl (or coretemp
    Package id), averaged over two reads one sample apart. dimm_temp_c is its hottest spd5118 DIMM.
    """

    observed_at: str
    host: str
    cpu_temp_c: float | None
    dimm_temp_c: float | None
    load1: float | None
    load5: float | None
    load15: float | None
    cpu_busy_pct: float | None
    cpu_iowait_pct: float | None
    mem_available_gb: float | None
    swap_used_gb: float | None
    gpus: tuple[ModelHostGpuReading, ...] = ()
    fans_rpm: tuple[tuple[str, int], ...] = ()
    errors: tuple[str, ...] = ()

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    def line(self) -> str:
        cpu = "n/a" if self.cpu_temp_c is None else f"{self.cpu_temp_c:.1f}C"
        load = "n/a" if self.load1 is None else f"{self.load1:.2f}"
        busy = "n/a" if self.cpu_busy_pct is None else f"{self.cpu_busy_pct:.1f}%"
        gpu = (
            ",".join(f"{g.temp_c:.0f}C/{g.util_pct:.0f}%" for g in self.gpus) or "none"
        )
        return (
            f"host_thermal host={self.host} at={self.observed_at} cpu={cpu} load1={load} "
            f"busy={busy} gpu={gpu} fans={len(self.fans_rpm)}"
        )
