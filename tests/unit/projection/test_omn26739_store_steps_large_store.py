# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26739: a large local store opens in seconds, each step applied once, and a step past its bound fails typed.

An in-process delegate run on a store of about 202 MB spun one core past 200 s in _apply_store_steps. The cause:
recompute_run_savings resolved the counterfactual baseline for every run, and each resolution parses the pricing
manifest (about 150 ms), so the pass cost minutes per thousand runs.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from omnimarket.models.model_local_install_schema import (
    STORE_STEP_BOUND_ENV,
    StoreStepBoundExceededError,
)
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

pytestmark = pytest.mark.unit

_RUNS = 30_000
_STEP = "omn24615_delegation_events_run_savings_recompute"


def _large_store(tmp_path: Path, runs: int) -> Path:
    path = tmp_path / "delegation.sqlite"
    SqliteDatabaseAdapter(path)._connect().close()
    with sqlite3.connect(path) as conn:
        row = {
            "tenant_id": "t",
            "task_type": "document",
            "delegated_to": "Qwen3.8-27B",
            "model_name": "Qwen3.8-27B",
            "cost_usd": 0.0,
            "tokens_input": 213,
            "tokens_output": 6,
            "created_at": "2026-10-10T18:13:00.608068+00:00",
        }
        columns = ["correlation_id", *row]
        conn.executemany(
            f"INSERT INTO delegation_events ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' * len(columns))})",
            [(f"run-{i}", *row.values()) for i in range(runs)],
        )
        conn.execute("DELETE FROM omnimarket_sqlite_store_steps")
    return path


def test_apply_store_steps_large_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1: every pending step runs on a large recorded store in under 10 s, each once."""
    path = _large_store(tmp_path, _RUNS)
    from omnimarket.nodes.node_projection_metering_summary import baseline

    resolutions = 0
    real = baseline._resolve_from_manifest

    def counting(model_id: str) -> object:
        nonlocal resolutions
        resolutions += 1
        return real(model_id)

    monkeypatch.setattr(baseline, "_resolve_from_manifest", counting)
    started = time.monotonic()
    SqliteDatabaseAdapter(path)._connect().close()
    elapsed = time.monotonic() - started
    assert elapsed < 10, f"{_RUNS} runs took {elapsed:.1f} s"
    assert resolutions == 1  # the baseline is read once for the pass, not once per run
    with sqlite3.connect(path) as conn:
        steps = [
            r[0] for r in conn.execute("SELECT step FROM omnimarket_sqlite_store_steps")
        ]
        assert len(steps) == len(set(steps))
        assert _STEP in steps
        derived = conn.execute(
            "SELECT COUNT(*) FROM delegation_events WHERE savings_measurement IS NOT NULL"
        ).fetchone()[0]
    assert derived == _RUNS
    again = time.monotonic()
    SqliteDatabaseAdapter(path)._connect().close()
    assert time.monotonic() - again < 2  # a recorded store reads, it does not reapply


def test_apply_store_steps_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2: a step that cannot finish within its bound fails typed, names itself, and records nothing."""
    path = _large_store(tmp_path, 5_000)
    monkeypatch.setenv(STORE_STEP_BOUND_ENV, "0.000001")
    with pytest.raises(StoreStepBoundExceededError) as raised:
        SqliteDatabaseAdapter(path)._connect().close()
    failed = raised.value.step
    assert failed in str(raised.value)
    assert raised.value.bound_seconds == pytest.approx(0.000001)
    monkeypatch.delenv(STORE_STEP_BOUND_ENV)
    with sqlite3.connect(path) as conn:
        recorded = [
            r[0] for r in conn.execute("SELECT step FROM omnimarket_sqlite_store_steps")
        ]
    assert (
        recorded == []
    )  # rolled back whole: nothing recorded, a later open applies it
    assert failed not in recorded
    SqliteDatabaseAdapter(path)._connect().close()
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT 1 FROM omnimarket_sqlite_store_steps WHERE step = ?", (_STEP,)
        ).fetchone()
