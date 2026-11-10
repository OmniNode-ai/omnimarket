# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fixtures of the remote-lane stop tests: receipts, a ledger with a CLAIM, a fake ledger tool."""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest

LANE = "fake-lane-1"
RUN_ID = "rlane-fake-lane-1-aaa111"
CLAIM_TS = "2026-11-02T10:00:00Z"

# The ledger tool the real adapter calls: ``row TERMINAL ... --ledger <file>``. Like the real
# writer it refuses a second TERMINAL for one closed CLAIM (exit 65, nothing written).
LEDGER_TOOL = textwrap.dedent(
    """
    import sys
    args = sys.argv[1:]
    assert args[:2] == ["row", "TERMINAL"], args
    cells = {}
    kvs = []
    i = 2
    while i < len(args):
        key = args[i]
        if key == "--kv":
            kvs.append(args[i + 1])
        else:
            cells[key] = args[i + 1]
        i += 2
    path = cells["--ledger"]
    rows = open(path, encoding="utf-8").read().splitlines()
    closes = cells["--closes-claim"]
    if any("| TERMINAL |" in r and f"closes-CLAIM={closes}" in r for r in rows):
        print("REFUSED 65 the CLAIM is already closed by a TERMINAL")
        sys.exit(65)
    ts = "2026-11-02T10:30:00Z"
    parts = [ts, "TERMINAL", f"lane={cells['--lane']}", f"ticket={cells['--ticket']}"]
    parts += kvs + [f"outcome={cells['--outcome']}", f"closes-CLAIM={closes}"]
    parts += [f"friction={cells['--friction']}", cells["--text"]]
    open(path, "a", encoding="utf-8").write(" | ".join(parts) + "\\n")
    print(ts)
    """
)


@pytest.fixture
def ledger_tool(tmp_path: Path) -> list[str]:
    tool = tmp_path / "fake_ledger_tool.py"
    tool.write_text(LEDGER_TOOL, encoding="utf-8")
    return [sys.executable, str(tool)]


@pytest.fixture
def ledger(tmp_path: Path) -> Path:
    path = tmp_path / "ROLLING_WORK_LEDGER.md"
    path.write_text(
        f"{CLAIM_TS} | CLAIM | lane={LANE} | ticket=OMN-1 | host=h9 | run={RUN_ID} | "
        "worktree=omni_worktrees/OMN-1/repo | actor=claude | model=sonnet | est ~1 lane-hours\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def receipts(tmp_path: Path) -> Path:
    return tmp_path / "remote-lanes"


def write_receipt(receipts: Path, **fields: object) -> Path:
    lane = str(fields.get("lane", LANE))
    run_id = str(fields.get("run_id", RUN_ID))
    receipt: dict[str, object] = {
        "lane": lane,
        "run_id": run_id,
        "status": "running",
        "final": False,
        "started_at": "2026-11-02T10:00:01Z",
        **fields,
    }
    path = receipts / lane / f"{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt), encoding="utf-8")
    return path
