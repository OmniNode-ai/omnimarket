# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Concurrent binding observations must not share a temporary state file (OMN-19998)."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from omnimarket.handlers.bus_binding import recovery


@pytest.mark.unit
def test_concurrent_record_writers(tmp_path: Path) -> None:
    workers = 16
    iterations = 50
    barrier = Barrier(workers)

    def write_records(writer: int) -> None:
        barrier.wait(timeout=30)
        for iteration in range(iterations):
            recovery._write_record(
                tmp_path,
                "dev",
                {"lane": "dev", "writer": writer, "iteration": iteration},
            )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(write_records, writer) for writer in range(workers)]
        for future in futures:
            future.result(timeout=60)

    path = recovery.state_path(tmp_path, "dev")
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["lane"] == "dev"
    assert record["writer"] in range(workers)
    assert record["iteration"] == iterations - 1
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.unit
def test_serialization_failure_removes_temporary_file(tmp_path: Path) -> None:
    recovery._write_record(tmp_path, "dev", {"lane": "dev"})
    path = recovery.state_path(tmp_path, "dev")
    original = path.read_text(encoding="utf-8")

    with pytest.raises(TypeError, match="not JSON serializable"):
        recovery._write_record(tmp_path, "dev", {"invalid": object()})

    assert path.read_text(encoding="utf-8") == original
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.unit
def test_replace_failure_removes_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recovery._write_record(tmp_path, "dev", {"lane": "dev"})
    path = recovery.state_path(tmp_path, "dev")
    original = path.read_text(encoding="utf-8")

    def fail_replace(src: Path, dst: Path) -> None:
        assert src.parent == dst.parent == path.parent
        raise PermissionError("injected replacement failure")

    monkeypatch.setattr(recovery.os, "replace", fail_replace)
    with pytest.raises(PermissionError, match="injected replacement failure"):
        recovery._write_record(tmp_path, "dev", {"lane": "dev", "updated": True})

    assert path.read_text(encoding="utf-8") == original
    assert list(path.parent.iterdir()) == [path]
