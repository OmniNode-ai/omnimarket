# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The factory floor step finished in 55 s in the foreground and 317 s in the Background class, on a Mac at load 29 (OMN-19998).

The measured parts of the 317 s, from a profile of the step: 122 s reading the lane receipts directory (110 s of it one
metadata read per directory, 19,273 directories), 101 s reading the ledger (47 s parsing it, 15 s of that in strptime for
about 250,000 timestamps), 37 s importing a package that a run without a Phoenix address never uses.
Each test pins one of those parts to the same answer the slower code gave, and the last one shows the import is gone.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.nodes.node_factory_floor_effect.source_loader import (
    TS_FMT,
    _ledger_ts,
    load_receipts,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 6, 3, 0, 0, tzinfo=UTC)


def write_receipt(directory: Path, lane: str, started: str) -> None:
    directory.mkdir(parents=True)
    (directory / f"rlane-{lane}-aaaaaa.json").write_text(
        json.dumps(
            {
                "lane": lane,
                "run_id": f"rlane-{lane}-aaaaaa",
                "host": "h051",
                "status": "running",
                "final": False,
                "started_at": started,
                "result": "",
            }
        ),
        encoding="utf-8",
    )


def test_golden_receipts_are_the_same_whatever_the_worker_count(tmp_path: Path) -> None:
    for i in range(40):
        write_receipt(
            tmp_path / f"lane-{i:02d}", f"lane-{i:02d}", "2026-11-06T02:00:00Z"
        )
    old = tmp_path / "old-lane"
    write_receipt(old, "old-lane", "2026-11-04T02:00:00Z")
    long_ago = NOW.timestamp() - 90 * 3600
    os.utime(old, (long_ago, long_ago))
    (tmp_path / "not-a-directory.json").write_text("{}", encoding="utf-8")

    def read(workers: int) -> list[str]:
        out = load_receipts(
            tmp_path,
            now=NOW,
            window_hours=24,
            glob="rlane-*.json",
            result_chars=600,
            scan_workers=workers,
        )
        assert out is not None
        return [r.lane for r in out]

    one = read(1)
    assert one == [f"lane-{i:02d}" for i in range(40)]
    assert "old-lane" not in one
    assert read(8) == one
    assert read(64) == one


@pytest.mark.parametrize(
    "stamp",
    [
        "2026-11-06T02:00:00Z",
        "2026-02-28T23:59:59Z",
        "2024-02-29T00:00:00Z",
        "2026-13-01T00:00:00Z",
        "2026-02-30T00:00:00Z",
        "2026-11-06T24:00:00Z",
        "2026-11-06T02:00:60Z",
        "2026-11-06T02:00:00",
        "2026-11-06 02:00:00Z",
        "2026-11-06T02:00:00+00:00",
        "2026-11-06T02:00:00.5Z",
        "2026-1-6T2:0:0Z",
        "+026-11-06T02:00:00Z",
        "2026-11-06T02:00:0 Z",
        "\uff12\uff10\uff12\uff16-11-06T02:00:00Z",
        "",
        "not a stamp",
    ],
)
def test_golden_the_fast_ledger_stamp_gives_the_answer_strptime_gave(
    stamp: str,
) -> None:
    try:
        expected: datetime | None = datetime.strptime(stamp, TS_FMT).replace(tzinfo=UTC)
    except ValueError:
        expected = None
    assert _ledger_ts(stamp) == expected


def test_error_the_import_of_the_effect_does_not_pull_in_the_infrastructure_package() -> (
    None
):
    """Without a Phoenix address the step never needs omnibase_infra, which cost 24 s to import in the Background class."""
    code = (
        "import sys, omnimarket.nodes.node_factory_floor_effect;"
        "import omnimarket.nodes.node_factory_floor_effect.phoenix_reader as r;"
        "assert 'omnibase_infra' not in sys.modules, 'omnibase_infra was imported';"
        "from datetime import UTC, datetime;"
        "assert r.load_phoenix_traces('', datetime.now(UTC)) is None;"
        "assert 'omnibase_infra' not in sys.modules, 'an empty address imported it'"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )
    assert done.returncode == 0, done.stderr[-500:]
