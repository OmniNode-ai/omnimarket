# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ledger-host snapshot script and the writer mutex it takes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from omnimarket.nodes.node_lab_stack_reaper_effect.handlers.handler_lab_stack_reaper import (
    SNAPSHOT_SCRIPT,
)
from omnimarket.nodes.node_lab_stack_reaper_effect.handlers.ledger_writer_lock import (
    LedgerWriterLock,
    lock_path_for,
)

pytestmark = pytest.mark.unit

CLAIM_ROW = (
    "2026-10-15T10:00:00Z | CLAIM | lane=worker | ticket=none | scratch evidence\n"
)


def _scratch(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    home.mkdir()
    (home / "ledger.md").write_text(CLAIM_ROW, encoding="utf-8")
    census = tmp_path / "census.yaml"
    census.write_text(
        "lanes:\n  dev:\n    compose_project: protected-dev\n", encoding="utf-8"
    )
    return home, census


def test_the_lock_path_is_a_function_of_the_resolved_ledger_alone(
    tmp_path: Path,
) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    one, other = tmp_path / "a" / "ledger.md", tmp_path / "b" / "ledger.md"
    assert lock_path_for(one) == lock_path_for(tmp_path / "a" / "." / "ledger.md")
    assert lock_path_for(one) != lock_path_for(other)
    assert lock_path_for(one).parent.name == ".ledger_locks"


def test_a_held_writer_lock_excludes_the_snapshot_until_it_times_out(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "ledger.md"
    ledger.write_text(CLAIM_ROW, encoding="utf-8")
    with (
        LedgerWriterLock(ledger, 1),
        pytest.raises(TimeoutError, match="timed out waiting"),
    ):
        LedgerWriterLock(ledger, 0).acquire()
    again = LedgerWriterLock(ledger, 0)
    again.acquire()
    again.release()
    assert lock_path_for(ledger).is_file()
    assert (lock_path_for(ledger).parent / ".gitignore").is_file()


def test_the_snapshot_script_prints_the_window_sources_and_the_census(
    tmp_path: Path,
) -> None:
    home, census = _scratch(tmp_path)
    done = subprocess.run(
        [sys.executable, "-c", f"exec({SNAPSHOT_SCRIPT!r})", "ledger.md"],
        env={**os.environ, "OMNI_HOME": str(home), "ONEX_LANE_MANIFEST": str(census)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    data = json.loads(done.stdout)
    assert data["ticket_inventory"] is None
    assert data["census"] == census.read_text(encoding="utf-8")
    assert [source["name"] for source in data["sources"]] == ["ledger.md"]
    assert data["sources"][0]["text"] == CLAIM_ROW


def test_the_snapshot_script_refuses_without_a_census(tmp_path: Path) -> None:
    home, _census = _scratch(tmp_path)
    env = {k: v for k, v in os.environ.items() if k != "ONEX_LANE_MANIFEST"}
    done = subprocess.run(
        [sys.executable, "-c", f"exec({SNAPSHOT_SCRIPT!r})", "ledger.md"],
        env={**env, "OMNI_HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode != 0
    assert not done.stdout.strip()
