# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A host takes a bounded burst of workers inside one settle window, whatever one probe reading says (OMN-19998).

2026-10-11 06:42-06:46Z: the landing controller placed five fix workers on n201 inside one tick, n201 rose to load1 94-101 on 32
cores, and every lab host then refused lanes. Each worker re-probes under the placement lock and sees the earlier ones as
``placed``, but a worker placed seconds ago is in neither the one-minute load average nor ``mem_avail``: the probe of the next
worker still reads a healthy host, so only ``cap`` (16 on n201) bounded the burst. Offline: the probe is a fixed reading."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from omnimarket.nodes.node_landing_placement_effect.handlers import (
    handler_landing_placement as lp,
)

BURST = 3
SETTLE_S = 120.0
# The reading every probe returns: a healthy 32-core host, load1 6, 60 GB free. A worker placed a moment ago changes none of it.
PROBE = "PROBE 32 6.00 62914560\nCOAUTHOR false\nLOGIN 1 1"


real_read_pool = lp.read_pool


def pool(*names: str) -> list[Any]:
    rows = [
        {
            "name": n,
            "target": f"198.51.100.{i}",
            "engines": ["claude_sonnet", "claude_opus"],
            "cap": 16,
        }
        for i, n in enumerate(names, start=1)
    ]
    return lp.load_pool({"LANDING_POOL_HOSTS": json.dumps(rows)})


def stale_probe(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, 0, PROBE, "")


def brief(lease_id: int) -> dict[str, Any]:
    return {
        "lease_id": lease_id,
        "brief": {"engine": "claude_sonnet", "lease_id": lease_id},
    }


def test_workers_that_have_settled_still_count_as_placed_and_never_as_a_burst(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "placement"
    now = datetime(2026, 10, 11, 7, 0, tzinfo=UTC)
    old, fresh, dead = (
        lp.mark(directory, "n201", i, pid)
        for i, pid in ((1, os.getpid()), (2, os.getpid()), (3, 999999))
    )
    stamp = (now - timedelta(seconds=SETTLE_S + 1)).timestamp()
    os.utime(old, (stamp, stamp))
    os.utime(fresh, (now.timestamp(), now.timestamp()))
    assert lp.placed_counts(directory) == {"n201": 2}
    assert lp.fresh_counts(directory, now=now) == {"n201": 1}
    assert not dead.exists()
    assert lp.fresh_counts(tmp_path / "absent", now=now) == {}


def test_read_pool_charges_the_fresh_workers_of_each_host_and_the_description_keeps_them() -> (
    None
):
    host = pool("n201")[0]
    reading = real_read_pool(
        [host], placed={"n201": 4}, fresh={"n201": 2}, capture=stale_probe
    )[0]
    assert (reading.placed, reading.fresh) == (4, 2)
    assert "fresh=2" in reading.describe()
    assert "fresh=" not in replace(reading, fresh=0).describe()
    parsed = lp.Reading.from_description(host, reading.describe())
    assert parsed is not None
    assert parsed.fresh == 2
    assert parsed.slots == reading.slots
