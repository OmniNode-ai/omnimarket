# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The factory_floor step's cold ledger window read finishes inside the step's limit when the overlay names no worker count.

Recorded on the publishing Mac (err log, 2026-11-16T20:25Z): ``factory_floor step did not finish within 45 s``. The step's longest wait is
the window read of the ledger: one reader subprocess per split archive inside the 14-day window, and an archive's rows are cached per process
(or per Valkey entry), so a cold cycle reads every archive. Measured on lab host h051 with the ledger as of 2026-11-16 (15 archives in the
window, 185,739 rows, the file source): 38.5 s read one archive at a time (36.5 s in the archive reads), 20.2 s four at a time.

The overlay that names the worker count (``ledger_archives.read_workers``) was a separate, still unmerged overlays change, so the deployed
overlay named none and the read ran serially. The contract now declares the count the read uses when the overlay names none. Break: the same
overlay with the code before this change reads the archives one after another, and this test sees no two reads overlap.
"""

from __future__ import annotations

import json
import math
import re
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_factory_floor_effect.handlers.handler_factory_floor_effect import (
    HandlerFactoryFloorEffect,
)
from omnimarket.nodes.node_factory_floor_effect.models.model_factory_floor_run import (
    ModelFactoryFloorRunRequest,
)
from omnimarket.nodes.node_factory_floor_effect.source_loader import (
    OWN_CONTRACT_PATH,
    load_contract,
)
from tests.nodes.node_factory_floor_effect.test_line_board_facts import (
    EVIDENCE,
    OVERLAY,
    _prepare,
    _Reader,
)
from tests.nodes.node_factory_floor_effect.test_runtime_factory_floor import NOW, world
from tests.nodes.node_source_read_cache_effect.support import memory_reader

pytestmark = pytest.mark.unit

__all__ = ["world"]

# The line the publisher's err log recorded for the step, and the figures measured on lab host h051 (the lab ledger of 2026-11-16).
RECORDED_ERR_LINE = "factory_floor step did not finish within 45 s"
ARCHIVES_IN_WINDOW = 15
SECONDS_PER_ARCHIVE_READ = 36.5 / ARCHIVES_IN_WINDOW
WARM_AND_LIVE_SECONDS = 12.0
ARCHIVE_COUNT = 12
READ_SECONDS = 0.3
# The overlay the Mac ran: the archive block with no worker count and no cache time (overlays at d664453).
ARCHIVE_BLOCK = {
    "ledger_path_env": "TEST_LEDGER_PATH",
    "ledger_record_env": "TEST_LEDGER_RECORD",
    "archive_subdir": "archive",
    "archive_glob": "SCRATCH_LEDGER_*-split.md",
    "archive_date_pattern": r"\d{4}-\d{2}-\d{2}",
}


def _recorded_limit_seconds() -> int:
    named = re.search(r"did not finish within (\d+) s", RECORDED_ERR_LINE)
    assert named is not None
    return int(named.group(1))


def _recording_plugin(tmp: Path, log: Path) -> Path:
    """A ledger reader that takes ``READ_SECONDS`` per read and logs when each archive read started and ended."""
    plugin = tmp / "recording-plugin"
    (plugin / "scripts").mkdir(parents=True)
    (plugin / "scripts" / "onex_ledger.py").write_text(
        "import json, sys, time\n"
        "args = sys.argv[1:]\n"
        "name = args[args.index('--ledger') + 1].rsplit('/', 1)[-1]\n"
        "start = time.monotonic()\n"
        f"time.sleep({READ_SECONDS!r})\n"
        f"open({str(log)!r}, 'a').write(json.dumps([name, start, time.monotonic()]) + '\\n')\n"
        "print('ROWS n=0')\n",
        encoding="utf-8",
    )
    return plugin


def _peak_overlap(log: Path) -> tuple[int, int]:
    """The most archive reads that ran at once, and how many archive reads there were."""
    spans = [
        (start, end)
        for name, start, end in map(json.loads, log.read_text().splitlines())
        if name.endswith("-split.md")
    ]
    peak = max(sum(1 for s, e in spans if s <= start < e) for start, _ in spans)
    return peak, len(spans)


def _cold_cycle(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> tuple[int, int]:
    """One cold cycle of the floor effect under the Mac's overlay: an empty cache, every archive read from its file."""
    files = _prepare(world, monkeypatch)
    files["overlay"].write_text(
        yaml.safe_dump(
            {**OVERLAY, "ledger_source": "file", "ledger_archives": ARCHIVE_BLOCK}
        ),
        encoding="utf-8",
    )
    tmp = world["tmp"]
    live = tmp / "ledger" / "SCRATCH_LEDGER.md"
    live.parent.mkdir()
    live.write_text("", encoding="utf-8")
    (live.parent / "archive").mkdir()
    first = (NOW - timedelta(days=13)).replace(hour=0, minute=0, second=0)
    for n in range(ARCHIVE_COUNT):
        day = (first + timedelta(days=n)).strftime("%Y-%m-%d")
        (live.parent / "archive" / f"SCRATCH_LEDGER_{day}-split.md").write_text(
            "", encoding="utf-8"
        )
    log = tmp / "reads.jsonl"
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(_recording_plugin(tmp, log)))
    monkeypatch.setenv("TEST_LEDGER_PATH", str(live))
    monkeypatch.setenv("TEST_LEDGER_RECORD", "file")
    cache, _ = memory_reader()
    HandlerFactoryFloorEffect(
        merged_running=_Reader(EVIDENCE), source_cache=cache
    ).handle(
        ModelFactoryFloorRunRequest(
            now=NOW,
            home=str(world["home"]),
            kb=str(world["kb"]),
            unresolved_rows_file=str(files["archive"]),
        )
    )
    return _peak_overlap(log)


def test_a_cold_window_read_under_the_overlay_with_no_worker_count_reads_archives_together(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Break: the archives read one after another (peak 1) are 38.5 s measured, past 45 s once the Mac is loaded."""
    peak, reads = _cold_cycle(world, monkeypatch)
    assert reads == ARCHIVE_COUNT
    assert peak > 1, (
        f"{reads} archives were read one at a time: {RECORDED_ERR_LINE!r} on a cold cycle"
    )


def test_the_declared_read_leaves_the_recorded_limit_a_margin_of_half_again(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The step's limit is not raised: the modelled cold read at the peak just seen, with the warm and live reads, stays inside it by 1.5 times."""
    peak, _ = _cold_cycle(world, monkeypatch)
    cold_seconds = (
        math.ceil(ARCHIVES_IN_WINDOW / peak) * SECONDS_PER_ARCHIVE_READ
        + WARM_AND_LIVE_SECONDS
    )
    assert cold_seconds * 1.5 <= _recorded_limit_seconds(), (
        f"{cold_seconds:.1f} s modelled at {peak} at once"
    )


def test_the_contract_declares_the_worker_count_and_a_cache_time_longer_than_the_backstop() -> (
    None
):
    runtime = load_contract(OWN_CONTRACT_PATH).runtime
    assert runtime.ledger_archive_read_workers > 1
    # An archive never changes once rolled, and its key holds its size and modification time: a day's wait is only the clean-up bound.
    assert runtime.ledger_archive_cache_ttl_seconds >= 86400
