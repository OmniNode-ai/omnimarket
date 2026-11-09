# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Two job-log filler ticks never share one state directory (OMN-23349).

On 2026-11-09 a tick launched from an older install tree ran 4 h 12 min at 80 to 100 percent of a core, orphaned
beside the current unit's own tick, both on the same state directory. A tick now takes an exclusive lock on
``tick.lock`` in its state directory before it reads or writes anything; a second one exits ``EXIT_LOCKED`` and leaves
the first one's evidence alone.
"""

from __future__ import annotations

import fcntl
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    load_units,
)
from omnimarket.nodes.node_job_log_filler_effect import (
    runtime_job_log_filler_tick as tick,
)
from omnimarket.nodes.node_job_log_filler_effect.handlers.adapter_job_log_readers import (
    InMemoryJobLogReader,
)
from omnimarket.nodes.node_job_log_filler_effect.handlers.handler_job_log_filler import (
    HandlerJobLogFiller,
)
from tests.nodes.node_job_log_filler_effect.planted import (
    RED_JOB,
    SUMMARY_JOB,
    overlay,
    transport_for,
    watcher_state,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 9, 8, 30, tzinfo=UTC)
# The lock file's name is spelled here, not read from the tick, so a rename that skips the unit's declaration fails.
LOCK_NAME = "tick.lock"


class Forbidden:
    """A handler a locked-out tick must never reach."""

    calls = 0

    @property
    def held(self) -> dict[int, float]:
        type(self).calls += 1
        return {}

    def fill(self, request: Any) -> Any:
        type(self).calls += 1
        raise AssertionError("a tick that lost the lock ran a pass")


def handler(tmp_path: Path) -> HandlerJobLogFiller:
    return HandlerJobLogFiller(
        overlay=overlay(),
        reader_factory=lambda _p: InMemoryJobLogReader(
            tmp_path / "job-logs",
            ttl_seconds=86400,
            tail_bytes=4096,
            transport_factory=lambda: transport_for(RED_JOB, SUMMARY_JOB),
        ),
        state_reader=watcher_state,
    )


def run(tmp_path: Path, pass_handler: Any) -> int:
    return tick.main(
        ["--omni-home", str(tmp_path), "--state-dir", str(tmp_path / "evidence")],
        handler=pass_handler,
        now=NOW,
    )


def test_a_tick_that_finds_the_state_dir_locked_runs_no_pass_and_leaves_the_evidence(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "evidence"
    state_dir.mkdir()
    (state_dir / tick.EVIDENCE_FILE).write_text('{"status":"first"}\n')
    Forbidden.calls = 0
    with (state_dir / LOCK_NAME).open("a+") as first:
        fcntl.flock(first.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        code = run(tmp_path, Forbidden())
    assert code == tick.EXIT_LOCKED
    assert Forbidden.calls == 0
    assert (state_dir / tick.EVIDENCE_FILE).read_text() == '{"status":"first"}\n'
    assert not (state_dir / tick.HELD_FILE).exists()


def test_a_tick_that_ends_frees_the_state_dir_for_the_next(tmp_path: Path) -> None:
    assert run(tmp_path, handler(tmp_path)) == 0
    with (tmp_path / "evidence" / LOCK_NAME).open("a+") as probe:
        fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert run(tmp_path, handler(tmp_path)) == 0
    reading = json.loads((tmp_path / "evidence" / tick.EVIDENCE_FILE).read_text())
    assert reading["considered"] == 2


def test_the_lock_names_its_holder_for_the_installer_to_stop(tmp_path: Path) -> None:
    state_dir = tmp_path / "evidence"
    with tick.single_run(state_dir) as held:
        assert held
        assert (state_dir / LOCK_NAME).read_text().startswith("pid=")
        with tick.single_run(state_dir) as second:
            assert not second


def test_the_unit_declares_the_lock_the_tick_takes() -> None:
    assert tick.LOCK_FILE == LOCK_NAME
    assert load_units()["job-log-filler"]["tick_lock_file"] == LOCK_NAME
