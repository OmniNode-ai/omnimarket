# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Summarise a host's thermal series: temperature by load bucket and by hour.

The operator question is whether the CPU runs cooler when the host is not loaded, so
the report groups readings by load1 per core and shows the per-hour trend that a drain would bend
downward.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from .handler_host_thermal_observe import SERIES_FILE

BAR_C = 85.0
# (label, upper bound of load1 per core), ascending; the last bucket is open.
LOAD_BUCKETS = (
    ("idle (<0.15/core)", 0.15),
    ("light (<0.5/core)", 0.5),
    ("busy (<1.0/core)", 1.0),
    ("loaded", float("inf")),
)


def load_series(state_dir: Path, since: str | None = None) -> list[dict[str, object]]:
    path = state_dir / SERIES_FILE
    if not path.is_file():
        return []
    rows: list[dict[str, object]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(row, dict)
            and row.get("cpu_temp_c") is not None
            and (since is None or str(row["observed_at"]) >= since)
        ):
            rows.append(row)
    return rows


def _stats(values: list[float]) -> str:
    avg = sum(values) / len(values)
    return f"n={len(values)} min={min(values):.1f} avg={avg:.1f} max={max(values):.1f}"


def report(state_dir: Path, cores: int, since: str | None = None) -> str:
    rows = load_series(state_dir, since)
    if not rows:
        return "no readings"
    first, last = rows[0]["observed_at"], rows[-1]["observed_at"]
    out = [f"readings={len(rows)} first={first} last={last} bar={BAR_C:.0f}C"]
    buckets: dict[str, list[float]] = defaultdict(list)
    hours: dict[str, list[float]] = defaultdict(list)
    over = 0
    for row in rows:
        temp = float(str(row["cpu_temp_c"]))
        over += temp > BAR_C
        load = row.get("load1")
        if load is not None:
            per_core = float(str(load)) / cores
            label = next(name for name, bound in LOAD_BUCKETS if per_core < bound)
            buckets[label].append(temp)
        hours[str(row["observed_at"])[:13] + "Z"].append(temp)
    out.append(f"over_bar={over}/{len(rows)}")
    out.append("cpu temp C by load1 per core:")
    out.extend(
        f"  {name}: {_stats(buckets[name])}"
        for name, _ in LOAD_BUCKETS
        if buckets[name]
    )
    out.append("cpu temp C by hour (UTC):")
    out.extend(f"  {hour}: {_stats(values)}" for hour, values in sorted(hours.items()))
    return "\n".join(out)
