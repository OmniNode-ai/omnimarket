# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The controller carry reads only the tail of the old controller's logs (OMN-23708).

On 2026-11-11 the first live observe run read the controller's whole tick log (304 MB) and decision log
(88 MB) on every 60 s pass. A pass needs the newest finished tick and that tick's decisions, so it reads
the last ``controller_tail_bytes`` of each file, a value the snapshot overlay declares. Each test names the
break that turns it red.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import IO, Any

import pytest

from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.snapshot_errors import (
    SnapshotSourceError,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.snapshot_sources import (
    LandingCarryStateDirSource,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.models.model_snapshot_overlay import (
    ModelSnapshotOverlay,
)
from tests.nodes.node_pr_landing_snapshot_effect.controller_inputs import (
    CONTROLLER_TICK,
    REPO,
    T,
    write_controller_state,
)
from tests.nodes.node_pr_landing_snapshot_effect.snapshot_inputs import overlay

pytestmark = pytest.mark.unit

TAIL = 65536
OLD_ROWS = 40_000


class _CountingFile:
    """A binary file whose reads are counted, so the bytes a pass reads are a measurement."""

    def __init__(self, inner: IO[bytes], counter: list[int]) -> None:
        self._inner = inner
        self._counter = counter

    def read(self, size: int = -1) -> bytes:
        data = self._inner.read(size)
        self._counter.append(len(data))
        return data

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._inner.seek(offset, whence)

    def __enter__(self) -> _CountingFile:
        return self

    def __exit__(self, *exc: object) -> None:
        self._inner.close()


def _source(directory: Path) -> LandingCarryStateDirSource:
    return LandingCarryStateDirSource(
        directory / "last-facts.json",
        directory / "ticks.jsonl",
        directory / "decisions.jsonl",
        sidecar_path=directory / "sidecar.json",
        max_age_seconds=900,
        tail_bytes=TAIL,
        now=lambda: T + timedelta(seconds=60),
    )


def _enlarge(directory: Path) -> tuple[Path, Path]:
    """The logs as the Mac holds them: a long history ahead of the finished tick's rows."""
    ticks = directory / "ticks.jsonl"
    decisions = directory / "decisions.jsonl"
    finished_tick = ticks.read_text(encoding="utf-8")
    finished_rows = decisions.read_text(encoding="utf-8")
    history_ticks = "".join(
        json.dumps({"ts": "2026-10-01T00:00:00Z", "tick": n - OLD_ROWS}) + "\n"
        for n in range(OLD_ROWS)
    )
    history_rows = "".join(
        json.dumps(
            {
                "ts": "2026-10-01T00:00:00Z",
                "tick": n - OLD_ROWS,
                "kind": "merge",
                "subject": f"{REPO}#{n}",
                "outcome": "merged",
            }
        )
        + "\n"
        for n in range(OLD_ROWS)
    )
    ticks.write_text(history_ticks + finished_tick, encoding="utf-8")
    decisions.write_text(history_rows + finished_rows, encoding="utf-8")
    return ticks, decisions


def test_a_large_synthetic_log_is_read_only_by_its_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_controller_state(tmp_path)
    small = _source(tmp_path).read_carry()
    ticks, decisions = _enlarge(tmp_path)
    whole = ticks.stat().st_size + decisions.stat().st_size
    assert whole > 40 * TAIL  # control: reading both files whole is far over the bound
    counted: list[int] = []
    real_open = Path.open

    def counting_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        handle = real_open(self, *args, **kwargs)
        if self in (ticks, decisions):
            return _CountingFile(handle, counted)
        return handle

    monkeypatch.setattr(Path, "open", counting_open)
    got = _source(tmp_path).read_carry()
    assert got.controller_tick == CONTROLLER_TICK
    assert got.logged == small.logged
    assert counted, "the logs were not read through the counted open"
    assert sum(counted) <= 2 * TAIL, (
        f"read {sum(counted)} bytes of {whole}; the bound is {TAIL} per file"
    )


def test_error_chain_a_tail_with_no_whole_tick_row_is_unread(tmp_path: Path) -> None:
    write_controller_state(tmp_path)
    (tmp_path / "ticks.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-11-11T02:00:00Z",
                "tick": CONTROLLER_TICK,
                "pad": "x" * (2 * TAIL),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(SnapshotSourceError, match="no finished tick"):
        _source(tmp_path).read_carry()


def test_error_chain_a_tail_inside_the_ticks_decisions_is_unread(
    tmp_path: Path,
) -> None:
    write_controller_state(tmp_path)
    rows = "".join(
        json.dumps(
            {
                "ts": "2026-11-11T02:00:00Z",
                "tick": CONTROLLER_TICK,
                "kind": "dispatch_worker",
                "subject": f"{REPO}#{n}",
                "outcome": "spawned",
                "pad": "x" * 200,
            }
        )
        + "\n"
        for n in range(2000)
    )
    (tmp_path / "decisions.jsonl").write_text(rows, encoding="utf-8")
    with pytest.raises(SnapshotSourceError, match="do not reach back before tick"):
        _source(tmp_path).read_carry()


def test_the_tail_bound_is_an_overlay_value_with_a_floor() -> None:
    base = overlay().model_dump()
    fields = {
        "controller_facts_path": "$HOME/f.json",
        "controller_ticks_path": "$HOME/t.jsonl",
        "controller_decisions_path": "$HOME/d.jsonl",
        "controller_max_age_seconds": 900,
        "controller_sidecar_path": "$HOME/s.json",
        "external_blocker_kinds": ("upstream_open",),
        "external_blocker_unbounded_kinds": ("upstream_open",),
        "external_gate_hours": 1.0,
    }
    ok = ModelSnapshotOverlay.model_validate(
        {**base, **fields, "controller_tail_bytes": 4194304}
    )
    assert ok.controller_tail_bytes == 4194304
    with pytest.raises(ValueError, match="come as a set"):
        ModelSnapshotOverlay.model_validate({**base, **fields})
    with pytest.raises(ValueError, match="controller_tail_bytes"):
        ModelSnapshotOverlay.model_validate(
            {**base, **fields, "controller_tail_bytes": 10}
        )
